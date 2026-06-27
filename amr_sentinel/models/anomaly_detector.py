"""
amr_sentinel/models/anomaly_detector.py
========================================
Anomaly detector for AMR-Sentinel.

What it does:
    Compares actual resistance rates (from resistance_records) against
    the TFT model's forecasts to detect statistically significant
    deviations. Two signal types are detected:

    1. trajectory_deviation — actual resistance rate deviates significantly
       from the model's forecast for a given pathogen-antibiotic-country
       triplet and year. Uses CUSUM (Cumulative Sum) control charts over
       model residuals per triplet to detect sustained shifts, and
       Shewhart sigma-threshold rules for single-point spikes.

    2. rate_spike — resistance rate increases by more than a configurable
       absolute threshold in a single year, regardless of forecast
       (catches rapid outbreaks that the model may have missed).

Key fix in this version:
    generate_all_forecasts now loads train_dataset.pkl and passes the
    resulting TimeSeriesDataSet object as reference_dataset to
    forecast_triplet on every call. This avoids the
    'dict has no attribute get_parameters' error that occurred when
    pytorch_forecasting stored dataset_parameters as a plain dict in
    model.hparams. The pickle is loaded once and reused across all
    triplets for efficiency.

Inputs:
    - PostgreSQL resistance_records table (actual observed rates)
    - data/models/tft_amr_sentinel.ckpt (trained TFT model)
    - data/models/train_dataset.pkl (pickled training TimeSeriesDataSet)
    - data/processed/features_train.parquet (encoder context for inference)

Outputs:
    - List of AnomalySignal objects — one per detected anomaly
    - Optionally written to the anomaly_signals table in PostgreSQL

External dependencies:
    pip install pytorch-forecasting sqlalchemy psycopg2-binary python-dotenv
"""

import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "anomaly_detector.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("anomaly_detector")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = (
    f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
PROCESSED_DIR = DATA_DIR / "processed"
MODELS_DIR = DATA_DIR / "models"

# CUSUM parameters
CUSUM_K = 0.02   # Allowance: detects shifts of 4+ percentage points
CUSUM_H = 0.05   # Decision threshold: ~5pp cumulative drift

# Shewhart: alert if residual > SHEWHART_SIGMA * std_dev of residuals
SHEWHART_SIGMA = 2.5

# Rate spike: alert if resistance increases by more than this in one year
RATE_SPIKE_THRESHOLD = 0.10   # 10 percentage point increase

# Minimum residuals history needed before CUSUM is reliable
MIN_RESIDUALS_FOR_CUSUM = 3

# Only flag anomalies for the most recent N years
RECENT_YEARS_WINDOW = 3


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class AnomalySignal:
    """Structured anomaly signal output from the detector.

    Maps directly to the alerts table schema and is the input to
    the severity scorer (Task 2.4) and triage agent (Task 3.1).

    Confidence interval fields (Task 5.5):
        forecast_lower_80 / forecast_upper_80 — 80% CI from TFT quantiles
        forecast_lower_50 / forecast_upper_50 — 50% CI (interquartile)
        A narrow CI with large deviation = strong signal.
        A wide CI with large deviation = uncertain signal.
    """
    pathogen_name: str
    antibiotic_name: str
    country_iso3: str
    year: int
    signal_type: str
    current_resistance: float
    forecasted_rate: float
    deviation_magnitude: float
    trend_direction: str
    trend_slope: float
    cusum_value: float = 0.0
    residuals_std: float = 0.0
    region_who: str = ""
    regional_mean: float = 0.0
    detected_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    data_source: str = ""
    # Confidence intervals from TFT quantile outputs (Task 5.5)
    forecast_lower_80: float = 0.0
    forecast_upper_80: float = 0.0
    forecast_lower_50: float = 0.0
    forecast_upper_50: float = 0.0


# ---------------------------------------------------------------------------
# Step 1: Load actual resistance data
# ---------------------------------------------------------------------------

def load_actual_data(engine) -> pd.DataFrame:
    """Load all resistance records from PostgreSQL for anomaly detection.

    Args:
        engine: SQLAlchemy engine.

    Returns:
        DataFrame with pathogen_name, antibiotic_name, country_iso3,
        region_who, year, resistance_rate, data_source.
    """
    query = text("""
        SELECT
            pathogen_name,
            antibiotic_name,
            country_iso3,
            region_who,
            year,
            resistance_rate,
            data_source
        FROM resistance_records
        WHERE resistance_rate IS NOT NULL
          AND year IS NOT NULL
        ORDER BY pathogen_name, antibiotic_name, country_iso3, year
    """)
    df = pd.read_sql(query, engine)
    logger.info("Loaded %d resistance records for anomaly detection.", len(df))
    return df


# ---------------------------------------------------------------------------
# Step 2: Generate TFT forecasts for all triplets
# ---------------------------------------------------------------------------

def generate_all_forecasts(
    actual_df: pd.DataFrame,
) -> dict[tuple[str, str, str], dict[int, dict]]:
    """Generate TFT forecasts for all triplets present in actual_df.

    Fix: loads train_dataset.pkl once and passes the TimeSeriesDataSet
    object as reference_dataset to every forecast_triplet call. This
    prevents the 'dict has no attribute get_parameters' error that
    occurred when using model.hparams["dataset_parameters"] directly.

    Args:
        actual_df: DataFrame of actual resistance records.

    Returns:
        Dict mapping (pathogen, antibiotic, country) ->
            {year -> {median, lower_80, upper_80, lower_50, upper_50}}
        Returns empty dict if model or pickle not found.
    """
    checkpoint_path = MODELS_DIR / "tft_amr_sentinel.ckpt"
    dataset_pkl_path = MODELS_DIR / "train_dataset.pkl"
    train_path = PROCESSED_DIR / "features_train.parquet"

    if not checkpoint_path.exists():
        logger.warning(
            "No model checkpoint at %s. Using statistical detection only.",
            checkpoint_path,
        )
        return {}

    if not dataset_pkl_path.exists():
        logger.warning(
            "No train_dataset.pkl at %s. Re-run training to generate it. "
            "Using statistical detection only.",
            dataset_pkl_path,
        )
        return {}

    if not train_path.exists():
        logger.warning(
            "No training data at %s. Using statistical detection only.",
            train_path,
        )
        return {}

    try:
        from amr_sentinel.models.trajectory_forecaster import (
            TFTNoInterpretation,
            prepare_dataframe,
            load_reference_dataset,
        )
        from amr_sentinel.models.ensemble_forecaster import (
            forecast_triplet_ensemble as forecast_triplet,
        )

        logger.info("Loading TFT model and reference dataset...")
        model = TFTNoInterpretation.load_from_checkpoint(str(checkpoint_path))
        model.eval()

        # Load reference dataset ONCE — reused for all triplet forecasts.
        # This is the key fix: we pass a real TimeSeriesDataSet object
        # instead of relying on model.hparams["dataset_parameters"].
        reference_dataset = load_reference_dataset(dataset_pkl_path)

        train_df = pd.read_parquet(train_path)
        train_df = prepare_dataframe(train_df)

        # Check forecast cache before running the expensive TFT+ARIMA loop
        try:
            from amr_sentinel.models.forecast_cache import get_or_compute_forecasts
            logger.info("Checking forecast cache...")
            forecasts = get_or_compute_forecasts(model, train_df, reference_dataset)
            if forecasts:
                # Count how many of our actual triplets have forecasts
                actual_triplets = set(
                    tuple(r) for r in actual_df[
                        ["pathogen_name", "antibiotic_name", "country_iso3"]
                    ].drop_duplicates().values.tolist()
                )
                covered = len(actual_triplets & set(forecasts.keys()))
                total = len(actual_triplets)
                logger.info(
                    "TFT forecast coverage: %.1f%% (%d/%d triplets)",
                    covered / total * 100 if total > 0 else 0,
                    covered, total,
                )
                return forecasts
        except Exception as exc:
            logger.warning("Forecast cache unavailable (%s) — computing directly.", exc)

        triplets = (
            actual_df[["pathogen_name", "antibiotic_name", "country_iso3"]]
            .drop_duplicates()
            .values.tolist()
        )

        forecasts: dict[tuple, dict[int, dict]] = {}
        failed = 0

        logger.info("Generating TFT forecasts for %d triplets...", len(triplets))

        for pathogen, antibiotic, country in triplets:
            result = forecast_triplet(
                model=model,
                train_df=train_df,
                pathogen=pathogen,
                antibiotic=antibiotic,
                country_iso3=country,
                n_years_ahead=3,
                reference_dataset=reference_dataset,  # reused — no reload per call
            )
            if result is None:
                failed += 1
                continue
            triplet_key = (pathogen, antibiotic, country)
            fc_by_year = {fc["year"]: fc for fc in result["forecasts"]}
            # Store first forecast year's CI under special key "_ci"
            # These are used when building anomaly signals for this triplet
            if result["forecasts"]:
                first_fc = result["forecasts"][0]
                fc_by_year["_ci"] = {
                    "lower_80": first_fc.get("lower_80"),
                    "upper_80": first_fc.get("upper_80"),
                    "lower_50": first_fc.get("lower_50"),
                    "upper_50": first_fc.get("upper_50"),
                }
            forecasts[triplet_key] = fc_by_year

        logger.info(
            "Forecast generation complete. Success: %d | Failed: %d",
            len(forecasts), failed,
        )

        # Save to cache for future runs
        try:
            from amr_sentinel.models.forecast_cache import _compute_cache_key, _save_cache
            cache_key = _compute_cache_key()
            _save_cache(cache_key, forecasts)
        except Exception as exc:
            logger.warning("Could not save forecast cache: %s", exc)

        return forecasts

    except Exception as exc:
        logger.error("Forecast generation failed: %s", exc, exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# Step 3: CUSUM and residual helpers
# ---------------------------------------------------------------------------

def compute_residuals(
    triplet_df: pd.DataFrame,
    forecasts: dict[int, dict],
) -> pd.DataFrame:
    """Compute model residuals (actual - forecast) for a single triplet.

    Args:
        triplet_df: DataFrame for one triplet sorted by year.
        forecasts: Dict mapping year -> forecast dict for this triplet.

    Returns:
        triplet_df with forecasted_rate and residual columns added.
    """
    triplet_df = triplet_df.copy()
    triplet_df["forecasted_rate"] = triplet_df["year"].map(
        lambda y: forecasts.get(y, {}).get("median", np.nan)
    )
    triplet_df["forecast_lower_80"] = triplet_df["year"].map(
        lambda y: forecasts.get(y, {}).get("lower_80", np.nan)
    )
    triplet_df["forecast_upper_80"] = triplet_df["year"].map(
        lambda y: forecasts.get(y, {}).get("upper_80", np.nan)
    )
    triplet_df["forecast_lower_50"] = triplet_df["year"].map(
        lambda y: forecasts.get(y, {}).get("lower_50", np.nan)
    )
    triplet_df["forecast_upper_50"] = triplet_df["year"].map(
        lambda y: forecasts.get(y, {}).get("upper_50", np.nan)
    )
    triplet_df["residual"] = (
        triplet_df["resistance_rate"] - triplet_df["forecasted_rate"]
    )
    return triplet_df


def run_cusum(
    residuals: pd.Series,
    k: float = CUSUM_K,
    h: float = CUSUM_H,
) -> tuple[pd.Series, bool]:
    """Run a one-sided CUSUM control chart over residuals.

    Detects sustained upward shifts (resistance increasing beyond forecast).

    S_i = max(0, S_{i-1} + (residual_i - k))
    Alarm when S_i > h.

    Args:
        residuals: Series of (actual - forecast) values ordered by time.
        k: Allowance parameter.
        h: Decision threshold.

    Returns:
        Tuple of (cusum_series, alarm_triggered).
    """
    s = 0.0
    cusum_values = []

    for r in residuals:
        if np.isnan(r):
            cusum_values.append(np.nan)
            continue
        s = max(0.0, s + (r - k))
        cusum_values.append(s)

    cusum_series = pd.Series(cusum_values, index=residuals.index)
    valid = cusum_series.dropna()
    alarm = bool(valid.iloc[-1] > h) if len(valid) > 0 else False

    return cusum_series, alarm


def compute_trend(
    rate_series: pd.Series,
    window: int = 3,
) -> tuple[str, float]:
    """Compute trend direction and slope from last N years of data.

    Args:
        rate_series: Series of resistance rates ordered by year.
        window: Number of recent years to use.

    Returns:
        Tuple of (trend_direction, slope).
    """
    recent = rate_series.dropna().tail(window)
    if len(recent) < 2:
        return "stable", 0.0

    x = np.arange(len(recent), dtype=float)
    y = recent.values.astype(float)

    try:
        slope = float(np.polyfit(x, y, 1)[0])
    except (np.linalg.LinAlgError, ValueError):
        return "stable", 0.0

    if slope > 0.005:
        direction = "rising"
    elif slope < -0.005:
        direction = "falling"
    else:
        direction = "stable"

    return direction, round(slope, 6)


# ---------------------------------------------------------------------------
# Step 4: Statistical fallback forecast
# ---------------------------------------------------------------------------

def statistical_forecast(
    rate_series: pd.Series,
    year: int,
) -> tuple[float, float]:
    """Simple statistical forecast when TFT is unavailable.

    Uses 5-year rolling mean as baseline.

    Args:
        rate_series: Historical resistance rates ordered by year.
        year: Target year (unused — for API consistency).

    Returns:
        Tuple of (forecast_mean, forecast_std).
    """
    recent = rate_series.dropna().tail(5)
    if len(recent) < 2:
        val = float(recent.mean()) if len(recent) > 0 else 0.5
        return val, 0.05

    return float(recent.mean()), max(float(recent.std()), 0.01)


# ---------------------------------------------------------------------------
# Step 5: Regional context
# ---------------------------------------------------------------------------

def compute_regional_context(
    actual_df: pd.DataFrame,
    pathogen: str,
    antibiotic: str,
    region: str,
    year: int,
) -> float:
    """Compute mean resistance for peer countries in same region and year.

    Args:
        actual_df: Full resistance records DataFrame.
        pathogen, antibiotic, region, year: Filter keys.

    Returns:
        Regional mean resistance rate, or 0.0 if insufficient data.
    """
    peer_data = actual_df[
        (actual_df["pathogen_name"] == pathogen)
        & (actual_df["antibiotic_name"] == antibiotic)
        & (actual_df["region_who"] == region)
        & (actual_df["year"] == year)
        & (actual_df["resistance_rate"].notna())
    ]["resistance_rate"]

    return float(peer_data.mean()) if len(peer_data) > 0 else 0.0


# ---------------------------------------------------------------------------
# Step 6: Main detection logic
# ---------------------------------------------------------------------------

def detect_anomalies(
    actual_df: pd.DataFrame,
    forecasts: dict[tuple[str, str, str], dict[int, dict]],
    shewhart_sigma: float = SHEWHART_SIGMA,
    rate_spike_threshold: float = RATE_SPIKE_THRESHOLD,
    recent_years_window: int = RECENT_YEARS_WINDOW,
) -> list[AnomalySignal]:
    """Detect anomalies across all triplets using three detection rules.

    For each (pathogen, antibiotic, country) triplet:
        1. Compute residuals against TFT forecasts (or statistical baseline)
        2. Run CUSUM over residuals to detect sustained upward drift
        3. Apply Shewhart rule for single-point spikes
        4. Apply rate spike rule for rapid year-on-year increases
        5. Return AnomalySignal for any triggered alarm in recent years

    Args:
        actual_df: DataFrame of actual resistance records.
        forecasts: TFT forecast dict from generate_all_forecasts().
        shewhart_sigma: Sigma multiplier for single-point spike detection.
        rate_spike_threshold: Absolute threshold for year-on-year spike.
        recent_years_window: Only flag anomalies in this many recent years.

    Returns:
        List of AnomalySignal objects sorted by |deviation_magnitude| desc.
    """
    anomalies: list[AnomalySignal] = []
    max_year = int(actual_df["year"].max())
    min_flaggable_year = max_year - recent_years_window + 1

    grouped = actual_df.groupby(
        ["pathogen_name", "antibiotic_name", "country_iso3"], sort=False
    )

    for (pathogen, antibiotic, country), group in grouped:
        group = group.sort_values("year").reset_index(drop=True)
        triplet_key = (pathogen, antibiotic, country)
        region = (
            str(group["region_who"].iloc[0])
            if "region_who" in group.columns
            else ""
        )
        data_source = (
            str(group["data_source"].iloc[0])
            if "data_source" in group.columns
            else ""
        )

        triplet_forecasts = forecasts.get(triplet_key, {})
        use_tft = len(triplet_forecasts) > 0

        # Extract CI from first forecast year (stored under "_ci" key)
        triplet_ci = triplet_forecasts.get("_ci", {})
        ci_lower_80 = triplet_ci.get("lower_80")
        ci_upper_80 = triplet_ci.get("upper_80")
        ci_lower_50 = triplet_ci.get("lower_50")
        ci_upper_50 = triplet_ci.get("upper_50")

        if use_tft:
            group = compute_residuals(group, triplet_forecasts)
        else:
            forecast_means, forecast_stds = [], []
            for _, row in group.iterrows():
                history = group[group["year"] < row["year"]]["resistance_rate"]
                mean, std = statistical_forecast(history, int(row["year"]))
                forecast_means.append(mean)
                forecast_stds.append(std)
            group["forecasted_rate"] = forecast_means
            group["residual"] = (
                group["resistance_rate"] - group["forecasted_rate"]
            )

        valid_residuals = group["residual"].dropna()
        if len(valid_residuals) >= MIN_RESIDUALS_FOR_CUSUM:
            residuals_std = float(valid_residuals.std())
        else:
            residuals_std = 0.05

        cusum_series, cusum_alarm = run_cusum(group["residual"])
        trend_direction, trend_slope = compute_trend(group["resistance_rate"])

        recent_rows = group[group["year"] >= min_flaggable_year]

        for _, row in recent_rows.iterrows():
            year = int(row["year"])
            actual = float(row["resistance_rate"])
            forecast = (
                float(row["forecasted_rate"])
                if pd.notna(row.get("forecasted_rate"))
                else actual
            )
            residual = (
                float(row["residual"])
                if pd.notna(row.get("residual"))
                else 0.0
            )
            cusum_val = (
                float(cusum_series.dropna().iloc[-1])
                if len(cusum_series.dropna()) > 0
                else 0.0
            )

            signal_type = None

            # Rule 1: CUSUM alarm — sustained upward drift
            if cusum_alarm and year == max_year:
                signal_type = "trajectory_deviation"

            # Rule 2: Shewhart — single-point spike above sigma threshold
            elif (
                residuals_std > 0
                and residual > shewhart_sigma * residuals_std
                and residual > 0.03   # minimum 3pp absolute deviation
            ):
                signal_type = "trajectory_deviation"

            # Rule 3: Rate spike — rapid year-on-year increase
            else:
                prev_rows = group[group["year"] < year]
                if len(prev_rows) > 0:
                    prev_rate = float(prev_rows["resistance_rate"].iloc[-1])
                    if actual - prev_rate >= rate_spike_threshold:
                        signal_type = "rate_spike"

            if signal_type is None:
                continue

            regional_mean = compute_regional_context(
                actual_df, pathogen, antibiotic, region, year
            )

            anomaly = AnomalySignal(
                pathogen_name=pathogen,
                antibiotic_name=antibiotic,
                country_iso3=country,
                year=year,
                signal_type=signal_type,
                current_resistance=round(actual, 4),
                forecasted_rate=round(forecast, 4),
                deviation_magnitude=round(residual, 4),
                trend_direction=trend_direction,
                trend_slope=trend_slope,
                cusum_value=round(cusum_val, 4),
                residuals_std=round(residuals_std, 4),
                region_who=region,
                regional_mean=round(regional_mean, 4),
                data_source=data_source,
                forecast_lower_80=round(float(ci_lower_80), 4) if ci_lower_80 is not None else None,
                forecast_upper_80=round(float(ci_upper_80), 4) if ci_upper_80 is not None else None,
                forecast_lower_50=round(float(ci_lower_50), 4) if ci_lower_50 is not None else None,
                forecast_upper_50=round(float(ci_upper_50), 4) if ci_upper_50 is not None else None,
            )
            anomalies.append(anomaly)

    anomalies.sort(key=lambda a: abs(a.deviation_magnitude), reverse=True)

    logger.info(
        "Anomaly detection complete. Signals: %d "
        "(trajectory_deviation: %d, rate_spike: %d)",
        len(anomalies),
        sum(1 for a in anomalies if a.signal_type == "trajectory_deviation"),
        sum(1 for a in anomalies if a.signal_type == "rate_spike"),
    )

    return anomalies


# ---------------------------------------------------------------------------
# Step 7: Write anomalies to database
# ---------------------------------------------------------------------------

def write_anomalies_to_db(
    anomalies: list[AnomalySignal],
    engine,
    dry_run: bool = False,
) -> int:
    """Write detected anomaly signals to the anomaly_signals staging table.

    The triage agent (Task 3.1) reads from this table and decides which
    signals to escalate into full alerts.

    Args:
        anomalies: List of AnomalySignal objects.
        engine: SQLAlchemy engine.
        dry_run: If True, does not write to database.

    Returns:
        Number of signals written.
    """
    if dry_run or not anomalies:
        if dry_run:
            logger.info(
                "Dry run — %d anomalies not written.", len(anomalies)
            )
        return 0

    create_sql = text("""
        CREATE TABLE IF NOT EXISTS anomaly_signals (
            id                  SERIAL PRIMARY KEY,
            pathogen_name       TEXT NOT NULL,
            antibiotic_name     TEXT NOT NULL,
            country_iso3        TEXT NOT NULL,
            year                INTEGER NOT NULL,
            signal_type         TEXT NOT NULL,
            current_resistance  FLOAT,
            forecasted_rate     FLOAT,
            deviation_magnitude FLOAT,
            trend_direction     TEXT,
            trend_slope         FLOAT,
            cusum_value         FLOAT,
            residuals_std       FLOAT,
            region_who          TEXT,
            regional_mean       FLOAT,
            data_source         TEXT,
            detected_at         TIMESTAMP WITH TIME ZONE NOT NULL,
            UNIQUE (
                pathogen_name, antibiotic_name, country_iso3,
                year, signal_type
            )
        )
    """)

    insert_sql = text("""
        INSERT INTO anomaly_signals (
            pathogen_name, antibiotic_name, country_iso3, year,
            signal_type, current_resistance, forecasted_rate,
            deviation_magnitude, trend_direction, trend_slope,
            cusum_value, residuals_std, region_who, regional_mean,
            data_source, detected_at
        ) VALUES (
            :pathogen_name, :antibiotic_name, :country_iso3, :year,
            :signal_type, :current_resistance, :forecasted_rate,
            :deviation_magnitude, :trend_direction, :trend_slope,
            :cusum_value, :residuals_std, :region_who, :regional_mean,
            :data_source, :detected_at
        )
        ON CONFLICT (pathogen_name, antibiotic_name, country_iso3, year, signal_type)
        DO UPDATE SET
            current_resistance  = EXCLUDED.current_resistance,
            deviation_magnitude = EXCLUDED.deviation_magnitude,
            trend_direction     = EXCLUDED.trend_direction,
            cusum_value         = EXCLUDED.cusum_value,
            detected_at         = EXCLUDED.detected_at
    """)

    written = 0
    with engine.begin() as conn:
        conn.execute(create_sql)
        for anomaly in anomalies:
            try:
                conn.execute(insert_sql, asdict(anomaly))
                written += 1
            except Exception as exc:
                logger.error(
                    "Failed to write anomaly %s/%s/%s/%d: %s",
                    anomaly.pathogen_name, anomaly.antibiotic_name,
                    anomaly.country_iso3, anomaly.year, exc,
                )

    logger.info("Wrote %d anomaly signals to database.", written)
    return written


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_anomaly_detection(
    dry_run: bool = False,
    write_to_db: bool = True,
) -> list[AnomalySignal]:
    """Run the full anomaly detection pipeline.

    Args:
        dry_run: If True, skips database writes.
        write_to_db: If False, skips database writes regardless of dry_run.

    Returns:
        List of AnomalySignal objects sorted by |deviation_magnitude| desc.
    """
    started_at = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("Anomaly detection started: %s", started_at.isoformat())

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    actual_df = load_actual_data(engine)
    forecasts = generate_all_forecasts(actual_df)

    n_triplets = len(
        actual_df[["pathogen_name", "antibiotic_name", "country_iso3"]]
        .drop_duplicates()
    )
    tft_coverage = len(forecasts) / n_triplets if n_triplets > 0 else 0.0
    logger.info(
        "TFT forecast coverage: %.1f%% (%d/%d triplets)",
        tft_coverage * 100, len(forecasts), n_triplets,
    )

    anomalies = detect_anomalies(actual_df, forecasts)

    if write_to_db and not dry_run and anomalies:
        write_anomalies_to_db(anomalies, engine, dry_run=dry_run)

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    logger.info("-" * 60)
    logger.info("Anomaly detection complete in %.1fs", elapsed)
    logger.info("  Total anomalies : %d", len(anomalies))
    if anomalies:
        a = anomalies[0]
        logger.info(
            "  Top signal: %s / %s / %s "
            "(year=%d, deviation=%.3f, type=%s)",
            a.pathogen_name, a.antibiotic_name, a.country_iso3,
            a.year, a.deviation_magnitude, a.signal_type,
        )
    logger.info("=" * 60)

    return anomalies


# ---------------------------------------------------------------------------
# Standalone usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    anomalies = run_anomaly_detection(dry_run=True, write_to_db=False)

    print(f"\nTotal anomalies detected: {len(anomalies)}")

    if anomalies:
        print("\nTop 10 signals by deviation magnitude:")
        print(
            f"{'Pathogen':<25} {'Antibiotic':<28} {'Country':<8} "
            f"{'Year':<6} {'Actual':>8} {'Forecast':>10} "
            f"{'Deviation':>10} {'Type':<22} {'Trend'}"
        )
        print("-" * 130)
        for a in anomalies[:10]:
            print(
                f"{a.pathogen_name:<25} {a.antibiotic_name:<28} "
                f"{a.country_iso3:<8} {a.year:<6} "
                f"{a.current_resistance:>8.3f} {a.forecasted_rate:>10.3f} "
                f"{a.deviation_magnitude:>+10.3f} "
                f"{a.signal_type:<22} {a.trend_direction}"
            )

    sys.exit(0 if len(anomalies) >= 0 else 1)