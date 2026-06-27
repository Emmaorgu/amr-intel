"""
amr_sentinel/models/backtester.py
===================================
Backtesting module for AMR-Sentinel — Task 5.7.

PURPOSE
-------
Provides adversarial validation of the forecasting pipeline by:
    1. Re-running feature engineering with TRAIN_END_YEAR = 2019
    2. Retraining TFT on 2014-2019 data (separate checkpoint — never
       overwrites the production model)
    3. Generating ensemble forecasts for 2020, 2021, 2022
    4. Comparing against actual resistance_records for those years
    5. Producing a publishable scorecard

This is the most important remaining validation task before Abuja.
The lead-time proof ("detected X months before official recognition")
requires demonstrating the system would have flagged known crises
before they appeared in official ECDC annual reports.

WHY THIS MATTERS
----------------
The 5 manually seeded signal_validations give us "8 months lead time"
but they are hand-picked against a single report. Backtesting gives us:
    - Systematic coverage across all 803 triplets
    - MAE and directional accuracy per pathogen class
    - Specific examples: "System flagged Croatia CRKP in 2020.
      ECDC first noted it in the 2022 annual report."
    - A reproducible methodology that survives peer scrutiny

SCORECARD OUTPUT
----------------
    Overall MAE                 — mean absolute error across all triplets
    Directional accuracy        — % correct up/down forecasts
    Within-CI accuracy          — % actuals falling within 80% CI
    Per-pathogen MAE            — broken down by pathogen
    Top 10 lead-time detections — triplets flagged in 2020 that became
                                  critical by 2022/2023
    False positive rate         — flagged but did not materialise

BACKTEST ARTIFACTS
------------------
    data/models/backtest/tft_backtest_2019.ckpt
    data/models/backtest/train_dataset_backtest.pkl
    data/processed/backtest/features_train_2019.parquet
    data/processed/backtest/features_val_2019.parquet
    data/processed/backtest/features_test_2019.parquet
    data/backtest/backtest_results.json
    data/backtest/backtest_scorecard.txt

USAGE
-----
    python -m amr_sentinel.models.backtester
    python -m amr_sentinel.models.backtester --skip-training  (use existing backtest ckpt)
    python -m amr_sentinel.models.backtester --scorecard-only (just print scorecard from results)

Dependencies:
    All existing AMR-Sentinel dependencies + statsmodels
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv

warnings.filterwarnings("ignore", ".*Maximum Likelihood.*")
warnings.filterwarnings("ignore", ".*Non-stationary.*")
warnings.filterwarnings("ignore", ".*Non-invertible.*")

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths — all backtest artifacts go to separate directories
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
BACKTEST_DIR = DATA_DIR / "backtest"
BACKTEST_MODELS_DIR = DATA_DIR / "models" / "backtest"
BACKTEST_PROCESSED_DIR = DATA_DIR / "processed" / "backtest"

for d in [BACKTEST_DIR, BACKTEST_MODELS_DIR, BACKTEST_PROCESSED_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Backtest configuration
# ---------------------------------------------------------------------------

BACKTEST_TRAIN_END_YEAR = 2019   # train on 2014-2019
BACKTEST_VAL_YEAR = 2020         # validate on 2020
BACKTEST_TEST_YEARS = [2021, 2022]  # evaluate on 2021-2022
MIN_OBSERVATIONS_BACKTEST = 4    # lower threshold for 2014-2019 subset


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    """Single triplet backtest result for one forecast year."""
    pathogen_name: str
    antibiotic_name: str
    country_iso3: str
    forecast_year: int
    actual_rate: float
    forecast_median: float
    forecast_lower_80: float
    forecast_upper_80: float
    absolute_error: float
    direction_correct: bool        # Did forecast correctly predict up/down?
    within_80ci: bool              # Did actual fall within 80% CI?
    was_flagged: bool              # Would anomaly detector have flagged this?
    actual_became_critical: bool   # Did it reach critical threshold by 2022?


@dataclass
class BacktestScorecard:
    """Aggregate backtest performance metrics."""
    cutoff_year: int
    eval_years: list[int]
    n_triplets: int
    n_predictions: int

    # Core accuracy metrics
    overall_mae: float
    overall_rmse: float
    directional_accuracy: float    # % of up/down predictions correct
    within_80ci_rate: float        # % of actuals within 80% CI

    # Per-pathogen MAE
    per_pathogen_mae: dict[str, float]

    # Lead-time detection
    n_flagged_2020: int            # signals flagged in 2020
    n_confirmed_by_2022: int       # of those, how many were real
    lead_time_precision: float     # n_confirmed / n_flagged

    # Top lead-time discoveries
    top_detections: list[dict]     # top 10 triplets flagged early

    # Runtime
    computed_at: str
    training_elapsed_s: float
    eval_elapsed_s: float


# ---------------------------------------------------------------------------
# Step 1: Feature engineering with 2019 cutoff
# ---------------------------------------------------------------------------

def build_backtest_features() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Run feature engineering with TRAIN_END_YEAR=2019.

    Saves to data/processed/backtest/ to avoid touching production data.
    Returns (train_df, val_df, test_df) where:
        train = 2014-2019
        val   = 2020
        test  = 2021-2022
    """
    logger.info("Building backtest features with train cutoff 2019...")

    from sqlalchemy import create_engine, text

    DATABASE_URL = (
        f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    engine = create_engine(DATABASE_URL)

    query = text("""
        SELECT
            pathogen_name,
            antibiotic_name,
            country_iso3,
            region_who,
            year,
            resistance_rate,
            sample_count,
            data_source
        FROM resistance_records
        WHERE resistance_rate IS NOT NULL
          AND year <= 2022
        ORDER BY pathogen_name, antibiotic_name, country_iso3, year
    """)

    with engine.connect() as conn:
        raw_df = pd.read_sql(query, conn)

    logger.info("Loaded %d raw records (2014-2022) for backtest.", len(raw_df))

    # Import feature engineering functions
    from amr_sentinel.models.feature_engineering import (
        build_triplet_timeseries,
        engineer_features,
        encode_categoricals,
    )

    # Build per-triplet time series with feature engineering
    pivot_df = build_triplet_timeseries(raw_df)
    full_df = engineer_features(pivot_df)

    # Filter to triplets with enough pre-2020 data
    triplet_groups = full_df.groupby(["pathogen_name", "antibiotic_name", "country_iso3"])
    valid_triplets = [
        key for key, grp in triplet_groups
        if len(grp[grp["year"] <= BACKTEST_TRAIN_END_YEAR]) >= MIN_OBSERVATIONS_BACKTEST
    ]
    full_df = full_df[
        full_df.set_index(["pathogen_name", "antibiotic_name", "country_iso3"]).index.isin(valid_triplets)
    ].copy()

    if full_df.empty:
        raise ValueError("No triplets met minimum observation threshold for backtest.")

    logger.info("Backtest dataset: %d records, %d triplets",
                len(full_df), len(valid_triplets))

    # Encode categoricals — returns (df, encoding_dict)
    full_df, _ = encode_categoricals(full_df)

    # Assign time_idx (integer index per triplet for TFT)
    min_year = int(full_df["year"].min())
    full_df["time_idx"] = full_df["year"] - min_year

    # Time-based split
    train_df = full_df[full_df["year"] <= BACKTEST_TRAIN_END_YEAR].copy()
    val_df   = full_df[full_df["year"] == BACKTEST_VAL_YEAR].copy()
    test_df  = full_df[full_df["year"].isin(BACKTEST_TEST_YEARS)].copy()

    logger.info(
        "Backtest split — train: %d rows (%d-%d) | val: %d rows (%d) | test: %d rows (%s)",
        len(train_df), train_df["year"].min(), train_df["year"].max(),
        len(val_df), BACKTEST_VAL_YEAR,
        len(test_df), BACKTEST_TEST_YEARS,
    )

    # Save
    train_df.to_parquet(BACKTEST_PROCESSED_DIR / "features_train_2019.parquet", index=False)
    val_df.to_parquet(BACKTEST_PROCESSED_DIR / "features_val_2019.parquet", index=False)
    test_df.to_parquet(BACKTEST_PROCESSED_DIR / "features_test_2019.parquet", index=False)

    logger.info("Backtest features saved to %s", BACKTEST_PROCESSED_DIR)
    return train_df, val_df, test_df


# ---------------------------------------------------------------------------
# Step 2: Train backtest TFT model
# ---------------------------------------------------------------------------

def train_backtest_model(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    test_df: pd.DataFrame,
) -> tuple:
    """
    Train a TFT model on 2014-2019 data.

    Saves to data/models/backtest/ — never overwrites production model.

    Returns (model, trainer, checkpoint_path).
    """
    import numpy as np
    import lightning.pytorch as pl
    from pytorch_forecasting import TimeSeriesDataSet
    from pytorch_forecasting.data import GroupNormalizer
    from pytorch_forecasting.metrics import QuantileLoss
    from lightning.pytorch.callbacks import (
        EarlyStopping, ModelCheckpoint, LearningRateMonitor,
    )
    from lightning.pytorch.loggers import CSVLogger
    from amr_sentinel.models.trajectory_forecaster import (
        TFTNoInterpretation,
        ENCODER_LENGTH, PREDICTION_LENGTH, BATCH_SIZE,
        LEARNING_RATE, HIDDEN_SIZE, ATTENTION_HEAD_SIZE,
        DROPOUT, HIDDEN_CONTINUOUS_SIZE, QUANTILES,
        RANDOM_SEED, MAX_EPOCHS, EARLY_STOP_PATIENCE,
        EARLY_STOP_MIN_DELTA, GRADIENT_CLIP_VAL,
        TARGET, GROUP_IDS, STATIC_CATEGORICALS,
        TIME_VARYING_KNOWN_REALS, TIME_VARYING_UNKNOWN_REALS,
    )

    logger.info("Building backtest TFT datasets...")

    # Fill NaN lag/rolling features on input dataframes before concatenation.
    # Lag features (rate_lag1, rate_lag2) and rolling features (rate_roll3)
    # are NaN for earliest years — fill with forward/backfill within triplet.
    def _fill_lag_features(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        # Fill ALL numeric feature columns that have NaNs.
        # This covers rate_lag1, rate_lag2, rate_roll3, rate_yoy_delta,
        # regional_mean, trend features, and any other derived features.
        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        cols_with_nan = [c for c in numeric_cols if df[c].isna().any()
                         and c not in ["resistance_rate"]]  # never fill target
        for col in cols_with_nan:
            df[col] = (
                df.groupby(GROUP_IDS)[col]
                .transform(lambda x: x.ffill().bfill().fillna(0))
            )
        return df

    train_df = _fill_lag_features(train_df)
    val_df = _fill_lag_features(val_df)
    test_df = _fill_lag_features(test_df)

    # TFT requires string-typed categoricals, not integer codes
    train_val_df = pd.concat([train_df, val_df], ignore_index=True)
    for col in STATIC_CATEGORICALS:
        if col in train_val_df.columns:
            train_val_df[col] = train_val_df[col].astype(str)

    lag_cols = [c for c in train_val_df.columns if any(
        x in c for x in ["lag", "roll", "trend", "regional"]
    )]

    full_df_all = pd.concat([train_val_df, test_df], ignore_index=True)
    for col in STATIC_CATEGORICALS:
        if col in full_df_all.columns:
            full_df_all[col] = full_df_all[col].astype(str)
    max_train_val_idx = int(train_val_df["time_idx"].max())

    train_dataset = TimeSeriesDataSet(
        train_val_df[
            train_val_df["time_idx"] <= max_train_val_idx - PREDICTION_LENGTH
        ],
        time_idx="time_idx",
        target=TARGET,
        group_ids=GROUP_IDS,
        min_encoder_length=max(1, ENCODER_LENGTH // 2),
        max_encoder_length=ENCODER_LENGTH,
        min_prediction_length=1,
        max_prediction_length=PREDICTION_LENGTH,
        static_categoricals=STATIC_CATEGORICALS,
        static_reals=[],
        time_varying_known_reals=TIME_VARYING_KNOWN_REALS,
        time_varying_unknown_reals=TIME_VARYING_UNKNOWN_REALS,
        target_normalizer=GroupNormalizer(
            groups=GROUP_IDS,
            transformation="softplus",
        ),
        add_relative_time_idx=True,
        add_target_scales=True,
        add_encoder_length=True,
        allow_missing_timesteps=True,
    )

    val_dataset = TimeSeriesDataSet.from_dataset(
        train_dataset, train_val_df, predict=True, stop_randomization=True,
    )
    full_df_combined = pd.concat([train_val_df, test_df], ignore_index=True)
    for col in STATIC_CATEGORICALS:
        if col in full_df_combined.columns:
            full_df_combined[col] = full_df_combined[col].astype(str)
    test_dataset = TimeSeriesDataSet.from_dataset(
        train_dataset, full_df_combined, predict=True, stop_randomization=True,
    )

    train_loader = train_dataset.to_dataloader(
        train=True, batch_size=BATCH_SIZE, num_workers=0, shuffle=True,
    )
    val_loader = val_dataset.to_dataloader(
        train=False, batch_size=BATCH_SIZE * 2, num_workers=0, shuffle=False,
    )

    pl.seed_everything(RANDOM_SEED)
    model = TFTNoInterpretation.from_dataset(
        train_dataset,
        learning_rate=LEARNING_RATE,
        hidden_size=HIDDEN_SIZE,
        attention_head_size=ATTENTION_HEAD_SIZE,
        dropout=DROPOUT,
        hidden_continuous_size=HIDDEN_CONTINUOUS_SIZE,
        loss=QuantileLoss(quantiles=QUANTILES),
        optimizer="adam",
        log_interval=10,
        log_val_interval=1,
        reduce_on_plateau_patience=4,
    )

    logger.info("Training backtest TFT (2014-2019)...")

    checkpoint_callback = ModelCheckpoint(
        dirpath=str(BACKTEST_MODELS_DIR),
        filename="tft_backtest_2019",
        monitor="val_loss",
        mode="min",
        save_top_k=1,
    )
    early_stop = EarlyStopping(
        monitor="val_loss",
        min_delta=EARLY_STOP_MIN_DELTA,
        patience=EARLY_STOP_PATIENCE,
        mode="min",
    )
    lr_monitor = LearningRateMonitor(logging_interval="epoch")
    csv_logger = CSVLogger(
        save_dir=str(BACKTEST_MODELS_DIR), name="backtest_logs"
    )

    trainer = pl.Trainer(
        max_epochs=MAX_EPOCHS,
        accelerator="cpu",
        devices=1,
        gradient_clip_val=GRADIENT_CLIP_VAL,
        callbacks=[checkpoint_callback, early_stop, lr_monitor],
        logger=csv_logger,
        enable_progress_bar=True,
        log_every_n_steps=5,
        deterministic=True,
    )

    trainer.fit(model, train_dataloaders=train_loader, val_dataloaders=val_loader)

    # Save backtest dataset pkl
    bt_pkl_path = BACKTEST_MODELS_DIR / "train_dataset_backtest.pkl"
    with open(bt_pkl_path, "wb") as f:
        pickle.dump(train_dataset, f)
    logger.info("Saved backtest train_dataset to %s", bt_pkl_path)

    best_ckpt = checkpoint_callback.best_model_path
    logger.info("Backtest training complete. Best checkpoint: %s", best_ckpt)

    return model, trainer, best_ckpt, train_dataset


# ---------------------------------------------------------------------------
# Step 3: Generate backtest forecasts
# ---------------------------------------------------------------------------

def generate_backtest_forecasts(
    model,
    train_df: pd.DataFrame,
    train_dataset,
    target_years: list[int],
) -> dict[tuple, dict]:
    """
    Generate ensemble forecasts for backtest triplets.

    Uses the backtest model (trained to 2019) to forecast 2020-2022.
    Returns dict keyed by triplet with forecast values per year.
    """
    from amr_sentinel.models.ensemble_forecaster import forecast_triplet_ensemble

    triplets = list(
        train_df.groupby(["pathogen_name", "antibiotic_name", "country_iso3"])
        .groups.keys()
    )

    logger.info(
        "Generating backtest forecasts for %d triplets (target years: %s)...",
        len(triplets), target_years,
    )

    forecasts = {}
    failed = 0

    for i, (pathogen, antibiotic, country) in enumerate(triplets):
        result = forecast_triplet_ensemble(
            model=model,
            train_df=train_df,
            pathogen=pathogen,
            antibiotic=antibiotic,
            country_iso3=country,
            n_years_ahead=len(target_years) + 1,
            reference_dataset=train_dataset,
        )

        if result is None:
            failed += 1
            continue

        # Index forecasts by year
        fc_by_year = {fc["year"]: fc for fc in result["forecasts"]}
        forecasts[(pathogen, antibiotic, country)] = fc_by_year

        if (i + 1) % 100 == 0:
            logger.info("Backtest forecast progress: %d/%d", i + 1, len(triplets))

    logger.info(
        "Backtest forecasts complete. Success: %d | Failed: %d",
        len(forecasts), failed,
    )
    return forecasts


# ---------------------------------------------------------------------------
# Step 4: Evaluate against actuals
# ---------------------------------------------------------------------------

def evaluate_backtest(
    forecasts: dict,
    actual_df: pd.DataFrame,
    target_years: list[int],
) -> list[BacktestResult]:
    """
    Compare backtest forecasts against actual resistance rates.

    Args:
        forecasts: Dict from generate_backtest_forecasts().
        actual_df: Full resistance_records DataFrame (all years).
        target_years: Years to evaluate (2021, 2022).

    Returns:
        List of BacktestResult objects.
    """
    results = []
    critical_threshold = 0.50  # 50% resistance = critical

    # Build lookup: (pathogen, antibiotic, country, year) -> actual_rate
    actual_lookup = {}
    for _, row in actual_df.iterrows():
        key = (
            row["pathogen_name"],
            row["antibiotic_name"],
            row["country_iso3"],
            int(row["year"]),
        )
        actual_lookup[key] = float(row["resistance_rate"])

    # Check which triplets became critical by 2022
    critical_by_2022 = set()
    for (p, ab, c, yr), rate in actual_lookup.items():
        if rate >= critical_threshold:
            critical_by_2022.add((p, ab, c))

    for (pathogen, antibiotic, country), fc_by_year in forecasts.items():
        for year in target_years:
            actual_key = (pathogen, antibiotic, country, year)
            actual_rate = actual_lookup.get(actual_key)

            if actual_rate is None:
                continue

            fc = fc_by_year.get(year)
            if fc is None:
                continue

            median = float(fc.get("median", 0))
            lower_80 = float(fc.get("lower_80", 0))
            upper_80 = float(fc.get("upper_80", 1))

            abs_error = abs(actual_rate - median)
            within_ci = lower_80 <= actual_rate <= upper_80

            # Directional accuracy: did forecast correctly predict trend?
            # Compare median forecast to the last known rate (2019)
            last_known_key = (pathogen, antibiotic, country, BACKTEST_TRAIN_END_YEAR)
            last_known = actual_lookup.get(last_known_key)
            direction_correct = False
            if last_known is not None:
                forecast_up = median > last_known
                actual_up = actual_rate > last_known
                direction_correct = forecast_up == actual_up

            # Was it flagged? Simple threshold: forecast > last_known + 0.05
            was_flagged = (
                last_known is not None
                and median > last_known + 0.05
                and median > 0.10
            )

            became_critical = (pathogen, antibiotic, country) in critical_by_2022

            results.append(BacktestResult(
                pathogen_name=pathogen,
                antibiotic_name=antibiotic,
                country_iso3=country,
                forecast_year=year,
                actual_rate=round(actual_rate, 4),
                forecast_median=round(median, 4),
                forecast_lower_80=round(lower_80, 4),
                forecast_upper_80=round(upper_80, 4),
                absolute_error=round(abs_error, 4),
                direction_correct=direction_correct,
                within_80ci=within_ci,
                was_flagged=was_flagged,
                actual_became_critical=became_critical,
            ))

    logger.info("Evaluated %d backtest predictions.", len(results))
    return results


# ---------------------------------------------------------------------------
# Step 5: Build scorecard
# ---------------------------------------------------------------------------

def build_scorecard(
    results: list[BacktestResult],
    training_elapsed_s: float,
    eval_elapsed_s: float,
) -> BacktestScorecard:
    """Compute aggregate scorecard from backtest results."""
    if not results:
        raise ValueError("No backtest results to score.")

    errors = [r.absolute_error for r in results]
    squared_errors = [e ** 2 for e in errors]
    directions = [r.direction_correct for r in results]
    within_ci = [r.within_80ci for r in results]

    mae = sum(errors) / len(errors)
    rmse = (sum(squared_errors) / len(squared_errors)) ** 0.5
    dir_acc = sum(directions) / len(directions)
    ci_rate = sum(within_ci) / len(within_ci)

    # Per-pathogen MAE
    pathogen_errors: dict[str, list[float]] = {}
    for r in results:
        pathogen_errors.setdefault(r.pathogen_name, []).append(r.absolute_error)
    per_pathogen_mae = {
        p: round(sum(errs) / len(errs), 4)
        for p, errs in pathogen_errors.items()
    }

    # Lead-time detection
    flagged_2020 = [r for r in results if r.was_flagged and r.forecast_year == 2020]
    n_flagged = len(flagged_2020)
    n_confirmed = sum(1 for r in flagged_2020 if r.actual_became_critical)
    precision = n_confirmed / n_flagged if n_flagged > 0 else 0.0

    # Top detections — flagged in 2020, became critical, sorted by error asc
    top_detections = sorted(
        [
            {
                "pathogen": r.pathogen_name,
                "antibiotic": r.antibiotic_name,
                "country": r.country_iso3,
                "forecast_2020": f"{r.forecast_median*100:.1f}%",
                "actual_2022": f"{r.actual_rate*100:.1f}%",
                "error_pp": f"{r.absolute_error*100:.1f}pp",
                "within_ci": r.within_80ci,
            }
            for r in flagged_2020
            if r.actual_became_critical
        ],
        key=lambda x: float(x["error_pp"].replace("pp", "")),
    )[:10]

    triplets = set((r.pathogen_name, r.antibiotic_name, r.country_iso3) for r in results)

    return BacktestScorecard(
        cutoff_year=BACKTEST_TRAIN_END_YEAR,
        eval_years=sorted(set(r.forecast_year for r in results)),
        n_triplets=len(triplets),
        n_predictions=len(results),
        overall_mae=round(mae, 4),
        overall_rmse=round(rmse, 4),
        directional_accuracy=round(dir_acc, 4),
        within_80ci_rate=round(ci_rate, 4),
        per_pathogen_mae=per_pathogen_mae,
        n_flagged_2020=n_flagged,
        n_confirmed_by_2022=n_confirmed,
        lead_time_precision=round(precision, 4),
        top_detections=top_detections,
        computed_at=datetime.now(timezone.utc).isoformat(),
        training_elapsed_s=round(training_elapsed_s, 1),
        eval_elapsed_s=round(eval_elapsed_s, 1),
    )


# ---------------------------------------------------------------------------
# Print scorecard
# ---------------------------------------------------------------------------

def print_scorecard(sc: BacktestScorecard) -> None:
    """Print a human-readable backtesting scorecard."""
    print("\n" + "=" * 70)
    print("AMR-SENTINEL — BACKTESTING SCORECARD")
    print(f"Training cutoff: {sc.cutoff_year} | Evaluated: {sc.eval_years}")
    print("=" * 70)
    print(f"\nCOVERAGE")
    print(f"  Triplets evaluated     : {sc.n_triplets}")
    print(f"  Total predictions      : {sc.n_predictions}")
    print(f"\nACCURACY METRICS")
    print(f"  Overall MAE            : {sc.overall_mae*100:.2f}pp")
    print(f"  Overall RMSE           : {sc.overall_rmse*100:.2f}pp")
    print(f"  Directional accuracy   : {sc.directional_accuracy*100:.1f}%")
    print(f"  Within 80% CI rate     : {sc.within_80ci_rate*100:.1f}%")
    print(f"\nPER-PATHOGEN MAE")
    for pathogen, mae in sorted(sc.per_pathogen_mae.items(), key=lambda x: x[1]):
        print(f"  {pathogen[:40]:<40} : {mae*100:.2f}pp")
    print(f"\nLEAD-TIME DETECTION (signals flagged in {sc.cutoff_year+1})")
    print(f"  Signals flagged in {sc.cutoff_year+1}   : {sc.n_flagged_2020}")
    print(f"  Confirmed critical by {sc.eval_years[-1]}  : {sc.n_confirmed_by_2022}")
    print(f"  Lead-time precision    : {sc.lead_time_precision*100:.1f}%")
    if sc.top_detections:
        print(f"\nTOP LEAD-TIME DETECTIONS")
        print(f"  {'Triplet':<45} {'Fcst':>7} {'Actual':>7} {'Error':>8}")
        print(f"  {'-'*70}")
        for det in sc.top_detections:
            label = f"{det['pathogen'][:18]}/{det['antibiotic'][:10]}/{det['country']}"
            print(
                f"  {label:<45} {det['forecast_2020']:>7} "
                f"{det['actual_2022']:>7} {det['error_pp']:>8}"
            )
    print(f"\nRUNTIME")
    print(f"  Training elapsed       : {sc.training_elapsed_s/60:.1f} min")
    print(f"  Evaluation elapsed     : {sc.eval_elapsed_s:.1f}s")
    print(f"  Computed at            : {sc.computed_at[:19]}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_backtesting(
    skip_training: bool = False,
    scorecard_only: bool = False,
) -> BacktestScorecard:
    """
    Run the full backtesting pipeline.

    Args:
        skip_training: If True, load existing backtest checkpoint instead
                       of retraining. Useful for re-running evaluation.
        scorecard_only: If True, just load saved results and print scorecard.

    Returns:
        BacktestScorecard with all metrics.
    """
    import time

    if scorecard_only:
        results_path = BACKTEST_DIR / "backtest_results.json"
        if not results_path.exists():
            raise FileNotFoundError(
                f"No backtest results at {results_path}. "
                "Run without --scorecard-only first."
            )
        with open(results_path) as f:
            saved = json.load(f)
        results = [BacktestResult(**r) for r in saved["results"]]
        sc = build_scorecard(results, saved.get("training_elapsed_s", 0), 0)
        print_scorecard(sc)
        return sc

    # Step 1: Load actual data for evaluation
    logger.info("Loading resistance records for backtest evaluation...")
    from sqlalchemy import create_engine, text as sql_text
    DATABASE_URL = (
        f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        actual_df = pd.read_sql(
            sql_text("SELECT * FROM resistance_records WHERE resistance_rate IS NOT NULL"),
            conn,
        )
    logger.info("Loaded %d actual records.", len(actual_df))

    # Step 2: Build backtest features
    train_df, val_df, test_df = build_backtest_features()

    # Step 3: Train or load backtest model
    t_train_start = time.time()

    if skip_training:
        bt_ckpt = BACKTEST_MODELS_DIR / "tft_backtest_2019.ckpt"
        bt_pkl = BACKTEST_MODELS_DIR / "train_dataset_backtest.pkl"

        if not bt_ckpt.exists():
            raise FileNotFoundError(
                f"No backtest checkpoint at {bt_ckpt}. "
                "Run without --skip-training first."
            )
        if not bt_pkl.exists():
            raise FileNotFoundError(
                f"No backtest train_dataset at {bt_pkl}. "
                "Run without --skip-training first."
            )

        logger.info("Loading existing backtest model from %s...", bt_ckpt)
        from amr_sentinel.models.trajectory_forecaster import TFTNoInterpretation
        bt_model = TFTNoInterpretation.load_from_checkpoint(str(bt_ckpt))
        bt_model.eval()
        with open(bt_pkl, "rb") as f:
            train_dataset = pickle.load(f)
        training_elapsed = 0.0
        logger.info("Backtest model loaded.")
    else:
        bt_model, _, _, train_dataset = train_backtest_model(
            train_df, val_df, test_df
        )
        training_elapsed = time.time() - t_train_start
        logger.info("Backtest training complete in %.1f min.", training_elapsed / 60)

    # Step 4: Generate forecasts for 2020-2022
    t_eval_start = time.time()

    # Ensure string categoricals for TFT compatibility
    from amr_sentinel.models.trajectory_forecaster import STATIC_CATEGORICALS
    for col in STATIC_CATEGORICALS:
        if col in train_df.columns:
            train_df[col] = train_df[col].astype(str)

    forecasts = generate_backtest_forecasts(
        bt_model, train_df, train_dataset,
        target_years=[2020, 2021, 2022],
    )

    # Step 5: Evaluate
    results = evaluate_backtest(
        forecasts, actual_df,
        target_years=[2020, 2021, 2022],
    )

    eval_elapsed = time.time() - t_eval_start

    # Step 6: Build scorecard
    sc = build_scorecard(results, training_elapsed, eval_elapsed)

    # Save results
    results_path = BACKTEST_DIR / "backtest_results.json"
    with open(results_path, "w") as f:
        json.dump({
            "scorecard": asdict(sc),
            "results": [asdict(r) for r in results],
            "training_elapsed_s": training_elapsed,
        }, f, indent=2)
    logger.info("Backtest results saved to %s", results_path)

    # Save scorecard text
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_scorecard(sc)
    scorecard_text = buf.getvalue()
    with open(BACKTEST_DIR / "backtest_scorecard.txt", "w") as f:
        f.write(scorecard_text)

    print_scorecard(sc)
    return sc


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="AMR-Sentinel backtesting — Task 5.7"
    )
    parser.add_argument(
        "--skip-training",
        action="store_true",
        help="Skip TFT retraining, use existing backtest checkpoint",
    )
    parser.add_argument(
        "--scorecard-only",
        action="store_true",
        help="Load saved results and print scorecard without rerunning",
    )
    args = parser.parse_args()

    run_backtesting(
        skip_training=args.skip_training,
        scorecard_only=args.scorecard_only,
    )
