"""
amr_sentinel/models/feature_engineering.py
============================================
Feature engineering pipeline for the AMR-Sentinel trajectory forecaster.

What it does:
    Queries the resistance_records table and builds a clean, feature-rich
    time-series dataset per pathogen-antibiotic-country (PAC) triplet,
    ready for the Temporal Fusion Transformer (TFT) in Task 2.2.

Key responsibilities:
    1. Load raw resistance records from PostgreSQL
    2. Pivot to a per-triplet annual time series, filling gaps via
       forward/backward fill (short gaps) or linear interpolation
    3. Engineer lag features (1-year, 2-year), rolling averages (3-year),
       and regional trend aggregates
    4. Encode categorical variables (pathogen, antibiotic, country, WHO
       region, data source) as integer codes for the TFT
    5. Filter out triplets with fewer than MIN_OBSERVATIONS non-null years
    6. Perform a strictly time-based train/val/test split — no data leakage
    7. Save processed datasets to disk as Parquet files

Inputs:
    - PostgreSQL resistance_records table (via .env credentials)

Outputs:
    - data/processed/features_train.parquet
    - data/processed/features_val.parquet
    - data/processed/features_test.parquet
    - data/processed/feature_metadata.json  (encodings, split years, stats)

Split years (no data leakage):
    ECDC (2014-2024):  train=2014-2021, val=2022, test=2023-2024
    GLASS (2016-2023): train=2016-2021, val=2022, test=2023
    Combined:          train up to 2021, val=2022, test=2023+

External dependencies:
    pip install pandas sqlalchemy psycopg2-binary python-dotenv pyarrow
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

DATABASE_URL = (
    f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
PROCESSED_DIR = DATA_DIR / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# Minimum non-null observations a triplet must have to be included
MIN_OBSERVATIONS = 5

# Train / val / test split — strictly time-based
TRAIN_END_YEAR = 2021   # inclusive
VAL_YEAR = 2022
TEST_START_YEAR = 2023  # inclusive through max year in data

# Maximum gap (years) that will be filled via interpolation.
# Gaps longer than this cause the triplet to be filtered out.
MAX_INTERPOLATION_GAP = 3

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "feature_engineering.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("feature_engineering")

# ---------------------------------------------------------------------------
# WHO region groupings used for regional trend features
# ---------------------------------------------------------------------------

WHO_REGION_PEERS: dict[str, list[str]] = {
    "EURO": ["AUT", "BEL", "BGR", "CYP", "CZE", "DEU", "DNK", "EST", "FIN",
             "FRA", "GBR", "GRC", "HRV", "HUN", "IRL", "ISL", "ITA", "LIE",
             "LTU", "LUX", "LVA", "MLT", "MNE", "MKD", "NLD", "NOR", "POL",
             "PRT", "ROU", "SRB", "SVK", "SVN", "ESP", "SWE", "TUR", "UKR"],
    "AFRO": ["NGA", "GHA", "KEN", "ZAF", "ETH", "TZA", "UGA", "SEN", "CIV",
             "CMR", "ZWE", "ZMB", "MOZ", "AGO", "MDG", "MWI", "BWA", "NAM"],
    "AMRO": ["USA", "CAN", "BRA", "ARG", "MEX", "COL", "PER", "CHL", "ECU",
             "BOL", "PRY", "URY", "VEN", "GTM", "HND", "CRI", "PAN"],
    "SEARO": ["IND", "BGD", "IDN", "THA", "MMR", "NPL", "LKA", "MDV", "BTN",
              "TLS"],
    "WPRO": ["CHN", "JPN", "KOR", "AUS", "NZL", "PHL", "MYS", "VNM", "KHM",
             "LAO", "MNG", "PNG"],
    "EMRO": ["SAU", "ARE", "EGY", "IRN", "IRQ", "JOR", "KWT", "LBN", "MAR",
             "OMN", "PAK", "QAT", "SYR", "TUN", "YEM", "AFG", "DJI", "LBY",
             "SDN", "SOM", "PSE"],
}

# Build reverse map: ISO3 -> WHO region
ISO3_TO_REGION: dict[str, str] = {
    iso3: region
    for region, countries in WHO_REGION_PEERS.items()
    for iso3 in countries
}

# ---------------------------------------------------------------------------
# Step 1: Load raw data from PostgreSQL
# ---------------------------------------------------------------------------

def load_resistance_records(engine) -> pd.DataFrame:
    """Load all resistance records from PostgreSQL.

    Pulls only the columns needed for feature engineering. Records with
    no resistance_rate are retained at this stage — gaps are handled
    during the pivot step.

    Args:
        engine: SQLAlchemy engine connected to amr_sentinel database.

    Returns:
        DataFrame with columns:
        pathogen_name, antibiotic_name, country_iso3, region_who,
        year, resistance_rate, sample_count, data_source
    """
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
        WHERE year IS NOT NULL
        ORDER BY pathogen_name, antibiotic_name, country_iso3, year
    """)
    df = pd.read_sql(query, engine)
    logger.info("Loaded %d raw resistance records from database.", len(df))
    return df


# ---------------------------------------------------------------------------
# Step 2: Build per-triplet time series with gap filling
# ---------------------------------------------------------------------------

def build_triplet_timeseries(df: pd.DataFrame) -> pd.DataFrame:
    """Pivot raw records into a complete annual time series per triplet.

    For each (pathogen, antibiotic, country) triplet:
        - Creates a complete year range from min_year to max_year
        - Fills gaps up to MAX_INTERPOLATION_GAP years via linear
          interpolation
        - Forward/backward fills remaining edge gaps (1 year only)
        - Tags each row with how its value was obtained:
          "observed", "interpolated", or "filled"

    Triplets with fewer than MIN_OBSERVATIONS observed (non-null) values
    before gap filling are excluded entirely.

    Args:
        df: Raw resistance records DataFrame from load_resistance_records.

    Returns:
        DataFrame with one row per (pathogen, antibiotic, country, year),
        with resistance_rate gaps filled where possible, plus a
        'value_source' column tagging each row.

    Raises:
        ValueError: If the input DataFrame is empty.
    """
    if df.empty:
        raise ValueError("Input DataFrame is empty — no records to process.")

    # Fill missing region_who from our lookup map
    df = df.copy()
    df["region_who"] = df.apply(
        lambda r: r["region_who"] if pd.notna(r["region_who"])
        else ISO3_TO_REGION.get(r["country_iso3"], "UNKNOWN"),
        axis=1,
    )

    # Aggregate duplicates: if same triplet+year appears in multiple sources,
    # take the mean resistance_rate and max sample_count
    df_agg = (
        df.groupby(["pathogen_name", "antibiotic_name", "country_iso3", "year"])
        .agg(
            resistance_rate=("resistance_rate", "mean"),
            sample_count=("sample_count", "max"),
            region_who=("region_who", "first"),
            data_source=("data_source", lambda x: "+".join(sorted(x.unique()))),
        )
        .reset_index()
    )

    all_rows: list[dict] = []
    triplets_included = 0
    triplets_excluded = 0

    grouped = df_agg.groupby(
        ["pathogen_name", "antibiotic_name", "country_iso3"], sort=False
    )

    for (pathogen, antibiotic, country), group in grouped:
        group = group.sort_values("year").reset_index(drop=True)

        # Count observed (non-null) values before any filling
        observed_count = group["resistance_rate"].notna().sum()
        if observed_count < MIN_OBSERVATIONS:
            triplets_excluded += 1
            continue

        # Build complete year range
        min_year = int(group["year"].min())
        max_year = int(group["year"].max())
        full_years = pd.DataFrame({"year": range(min_year, max_year + 1)})

        merged = full_years.merge(group, on="year", how="left")
        merged["pathogen_name"] = pathogen
        merged["antibiotic_name"] = antibiotic
        merged["country_iso3"] = country
        merged["region_who"] = group["region_who"].iloc[0]
        merged["data_source"] = group["data_source"].iloc[0]

        # Tag value sources before filling
        merged["value_source"] = merged["resistance_rate"].apply(
            lambda v: "observed" if pd.notna(v) else "gap"
        )

        # Linear interpolation for gaps within data range
        # Only fills if the gap does not exceed MAX_INTERPOLATION_GAP
        rate_series = merged["resistance_rate"].copy()
        null_mask = rate_series.isna()

        if null_mask.any():
            # Find runs of consecutive nulls
            groups = (null_mask != null_mask.shift()).cumsum()
            for _, run in merged[null_mask].groupby(groups[null_mask]):
                run_len = len(run)
                if run_len <= MAX_INTERPOLATION_GAP:
                    # Mark these as interpolated before filling
                    merged.loc[run.index, "value_source"] = "interpolated"

            rate_series = rate_series.interpolate(
                method="linear", limit=MAX_INTERPOLATION_GAP,
                limit_direction="both"
            )
            # Forward/backward fill for edge gaps (1 year only)
            rate_series = rate_series.ffill(limit=1)
            rate_series = rate_series.bfill(limit=1)

            # Update source tags for newly filled values
            was_gap = merged["value_source"] == "gap"
            now_filled = rate_series.notna()
            merged.loc[was_gap & now_filled, "value_source"] = "filled"

        merged["resistance_rate"] = rate_series

        # Drop rows still missing resistance_rate after filling
        merged = merged.dropna(subset=["resistance_rate"])

        # Re-check minimum observations after filling
        if len(merged) < MIN_OBSERVATIONS:
            triplets_excluded += 1
            continue

        all_rows.extend(merged.to_dict("records"))
        triplets_included += 1

    logger.info(
        "Triplets included: %d | excluded (< %d obs): %d",
        triplets_included, MIN_OBSERVATIONS, triplets_excluded,
    )

    result = pd.DataFrame(all_rows)
    result["year"] = result["year"].astype(int)
    result["resistance_rate"] = result["resistance_rate"].clip(0.0, 1.0)

    return result


# ---------------------------------------------------------------------------
# Step 3: Engineer features
# ---------------------------------------------------------------------------

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add lag features, rolling averages, and regional trend features.

    Features added per triplet:
        rate_lag1       resistance_rate shifted 1 year back
        rate_lag2       resistance_rate shifted 2 years back
        rate_roll3      3-year rolling mean (min_periods=2)
        rate_roll5      5-year rolling mean (min_periods=3)
        rate_yoy_delta  year-on-year absolute change
        rate_trend      linear trend coefficient over last 3 years

    Regional features (computed across all countries in same WHO region
    for same pathogen+antibiotic combination, per year):
        regional_mean   mean resistance rate across peer countries
        regional_std    std dev of resistance rates across peers
        regional_rank   percentile rank of this country among peers

    Args:
        df: Per-triplet timeseries DataFrame from build_triplet_timeseries.

    Returns:
        DataFrame with all original columns plus engineered features.
    """
    df = df.sort_values(
        ["pathogen_name", "antibiotic_name", "country_iso3", "year"]
    ).reset_index(drop=True)

    # --- Per-triplet features ---
    feature_rows: list[pd.DataFrame] = []

    for (pathogen, antibiotic, country), group in df.groupby(
        ["pathogen_name", "antibiotic_name", "country_iso3"], sort=False
    ):
        g = group.sort_values("year").copy()
        r = g["resistance_rate"]

        g["rate_lag1"] = r.shift(1)
        g["rate_lag2"] = r.shift(2)
        g["rate_roll3"] = r.rolling(window=3, min_periods=2).mean()
        g["rate_roll5"] = r.rolling(window=5, min_periods=3).mean()
        g["rate_yoy_delta"] = r.diff(1)

        # 3-year linear trend: slope of linear fit over last 3 years
        def rolling_slope(series: pd.Series, window: int = 3) -> pd.Series:
            slopes = []
            for i in range(len(series)):
                if i < window - 1:
                    slopes.append(np.nan)
                    continue
                window_vals = series.iloc[i - window + 1: i + 1].values
                x = np.arange(len(window_vals), dtype=float)
                if np.any(np.isnan(window_vals)):
                    slopes.append(np.nan)
                else:
                    slope = np.polyfit(x, window_vals, 1)[0]
                    slopes.append(float(slope))
            return pd.Series(slopes, index=series.index)

        g["rate_trend"] = rolling_slope(r)

        feature_rows.append(g)

    df_features = pd.concat(feature_rows, ignore_index=True)

    # --- Regional features ---
    # For each (pathogen, antibiotic, year, region), compute stats across
    # all countries in that region
    regional = (
        df_features
        .groupby(["pathogen_name", "antibiotic_name", "year", "region_who"])
        ["resistance_rate"]
        .agg(regional_mean="mean", regional_std="std")
        .reset_index()
    )

    df_features = df_features.merge(
        regional,
        on=["pathogen_name", "antibiotic_name", "year", "region_who"],
        how="left",
    )

    # Percentile rank within region for this year
    def regional_rank(group: pd.DataFrame) -> pd.Series:
        return group["resistance_rate"].rank(pct=True)

    df_features["regional_rank"] = df_features.groupby(
        ["pathogen_name", "antibiotic_name", "year", "region_who"],
        group_keys=False,
    ).apply(regional_rank)

    # Fill std = 0 for regions with only one country
    df_features["regional_std"] = df_features["regional_std"].fillna(0.0)

    logger.info(
        "Feature engineering complete. Shape: %s", df_features.shape
    )
    return df_features


# ---------------------------------------------------------------------------
# Step 4: Encode categorical variables
# ---------------------------------------------------------------------------

def encode_categoricals(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Encode categorical columns as integer codes for TFT input.

    The TFT requires integer-encoded categoricals for its embedding layers.
    This function encodes and returns a mapping dict for decoding later.

    Columns encoded:
        pathogen_name, antibiotic_name, country_iso3,
        region_who, data_source, antibiotic_class (if present)

    Args:
        df: Feature-engineered DataFrame.

    Returns:
        Tuple of:
            - DataFrame with _code columns added for each categorical
            - Dict mapping column_name -> {value: code} for decoding

    """
    encodings: dict[str, dict] = {}
    df = df.copy()

    categorical_cols = [
        "pathogen_name", "antibiotic_name", "country_iso3",
        "region_who", "data_source",
    ]

    for col in categorical_cols:
        if col not in df.columns:
            continue
        unique_vals = sorted(df[col].dropna().unique().tolist())
        mapping = {val: idx for idx, val in enumerate(unique_vals)}
        encodings[col] = mapping
        df[f"{col}_code"] = df[col].map(mapping).astype("Int64")

    logger.info(
        "Encoded %d categorical columns. Cardinalities: %s",
        len(encodings),
        {col: len(enc) for col, enc in encodings.items()},
    )
    return df, encodings


# ---------------------------------------------------------------------------
# Step 5: Train / val / test split
# ---------------------------------------------------------------------------

def split_dataset(df: pd.DataFrame) -> tuple[
    pd.DataFrame, pd.DataFrame, pd.DataFrame
]:
    """Split the dataset strictly by time — no data leakage.

    Split years:
        Train: year <= TRAIN_END_YEAR (2021)
        Val:   year == VAL_YEAR       (2022)
        Test:  year >= TEST_START_YEAR (2023)

    Triplets that appear in val/test must also appear in train.
    Any triplet with no training data is excluded from all splits.

    Args:
        df: Fully featured and encoded DataFrame.

    Returns:
        Tuple of (train_df, val_df, test_df).
    """
    # Identify triplets present in training data
    train_triplets = set(
        df[df["year"] <= TRAIN_END_YEAR]
        .apply(
            lambda r: (r["pathogen_name"], r["antibiotic_name"], r["country_iso3"]),
            axis=1,
        )
        .unique()
    )

    df["_triplet"] = list(zip(
        df["pathogen_name"], df["antibiotic_name"], df["country_iso3"]
    ))
    df_with_train = df[df["_triplet"].isin(train_triplets)].copy()
    df_with_train = df_with_train.drop(columns=["_triplet"])

    train_df = df_with_train[df_with_train["year"] <= TRAIN_END_YEAR].copy()
    val_df = df_with_train[df_with_train["year"] == VAL_YEAR].copy()
    test_df = df_with_train[df_with_train["year"] >= TEST_START_YEAR].copy()

    excluded = len(df) - len(df_with_train)

    logger.info(
        "Split complete. Train: %d rows | Val: %d rows | Test: %d rows | "
        "Excluded (no train data): %d rows",
        len(train_df), len(val_df), len(test_df), excluded,
    )

    # Verify no data leakage
    assert train_df["year"].max() <= TRAIN_END_YEAR, "DATA LEAKAGE: train contains post-2021 data"
    assert val_df["year"].min() >= VAL_YEAR, "DATA LEAKAGE: val contains pre-2022 data"
    assert test_df["year"].min() >= TEST_START_YEAR, "DATA LEAKAGE: test contains pre-2023 data"
    logger.info("Data leakage checks passed.")

    return train_df, val_df, test_df


# ---------------------------------------------------------------------------
# Step 6: Save outputs
# ---------------------------------------------------------------------------

def save_outputs(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    test_df: pd.DataFrame,
    encodings: dict[str, dict],
    stats: dict,
) -> None:
    """Save processed datasets and metadata to disk.

    Args:
        train_df: Training split DataFrame.
        val_df: Validation split DataFrame.
        test_df: Test split DataFrame.
        encodings: Categorical encoding mappings.
        stats: Summary statistics dict for logging and reproducibility.
    """
    train_path = PROCESSED_DIR / "features_train.parquet"
    val_path = PROCESSED_DIR / "features_val.parquet"
    test_path = PROCESSED_DIR / "features_test.parquet"
    meta_path = PROCESSED_DIR / "feature_metadata.json"

    train_df.to_parquet(train_path, index=False)
    val_df.to_parquet(val_path, index=False)
    test_df.to_parquet(test_path, index=False)

    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "split_years": {
            "train_end": TRAIN_END_YEAR,
            "val": VAL_YEAR,
            "test_start": TEST_START_YEAR,
        },
        "min_observations": MIN_OBSERVATIONS,
        "max_interpolation_gap": MAX_INTERPOLATION_GAP,
        "encodings": encodings,
        "stats": stats,
        "output_files": {
            "train": str(train_path),
            "val": str(val_path),
            "test": str(test_path),
        },
        "columns": list(train_df.columns),
    }

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, default=str)

    logger.info("Saved train to %s (%d rows)", train_path, len(train_df))
    logger.info("Saved val to %s (%d rows)", val_path, len(val_df))
    logger.info("Saved test to %s (%d rows)", test_path, len(test_df))
    logger.info("Saved metadata to %s", meta_path)


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

def run_feature_engineering() -> dict:
    """Run the complete feature engineering pipeline end to end.

    Steps:
        1. Load raw resistance records from PostgreSQL
        2. Build per-triplet annual time series with gap filling
        3. Engineer lag, rolling, and regional features
        4. Encode categorical variables
        5. Split into train / val / test by time
        6. Save Parquet files and metadata JSON to data/processed/

    Returns:
        Summary statistics dict with row counts and triplet counts.
    """
    started_at = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("Feature engineering pipeline started: %s", started_at.isoformat())

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    # Step 1: Load
    raw_df = load_resistance_records(engine)

    # Step 2: Build timeseries
    timeseries_df = build_triplet_timeseries(raw_df)

    # Step 3: Feature engineering
    featured_df = engineer_features(timeseries_df)

    # Step 4: Encode categoricals
    encoded_df, encodings = encode_categoricals(featured_df)

    # Step 5: Split
    train_df, val_df, test_df = split_dataset(encoded_df)

    # Compute summary stats
    triplet_count = (
        train_df
        .groupby(["pathogen_name", "antibiotic_name", "country_iso3"])
        .ngroups
    )

    stats = {
        "total_rows_after_processing": len(encoded_df),
        "train_rows": len(train_df),
        "val_rows": len(val_df),
        "test_rows": len(test_df),
        "triplet_count": triplet_count,
        "pathogens": sorted(encoded_df["pathogen_name"].unique().tolist()),
        "antibiotics": sorted(encoded_df["antibiotic_name"].unique().tolist()),
        "countries": sorted(encoded_df["country_iso3"].unique().tolist()),
        "year_range": {
            "min": int(encoded_df["year"].min()),
            "max": int(encoded_df["year"].max()),
        },
        "null_rates_remaining": int(encoded_df["resistance_rate"].isna().sum()),
    }

    # Step 6: Save
    save_outputs(train_df, val_df, test_df, encodings, stats)

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    logger.info("-" * 60)
    logger.info("Feature engineering complete in %.1fs", elapsed)
    logger.info("  Total rows processed : %d", stats["total_rows_after_processing"])
    logger.info("  Triplets             : %d", stats["triplet_count"])
    logger.info("  Train rows           : %d", stats["train_rows"])
    logger.info("  Val rows             : %d", stats["val_rows"])
    logger.info("  Test rows            : %d", stats["test_rows"])
    logger.info("  Null resistance rates: %d", stats["null_rates_remaining"])
    logger.info("=" * 60)

    return stats


# ---------------------------------------------------------------------------
# Standalone usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    result = run_feature_engineering()
    print("\nFeature engineering summary:")
    print(f"  Triplets ready for TFT : {result['triplet_count']}")
    print(f"  Train rows             : {result['train_rows']}")
    print(f"  Val rows               : {result['val_rows']}")
    print(f"  Test rows              : {result['test_rows']}")
    print(f"  Output directory       : {PROCESSED_DIR}")
    sys.exit(0)