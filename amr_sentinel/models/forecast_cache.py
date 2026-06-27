"""
amr_sentinel/models/forecast_cache.py
=======================================
Forecast cache for AMR-Sentinel — eliminates the 18-minute TFT+ARIMA
bottleneck on repeated pipeline runs.

THE PROBLEM
-----------
Every pipeline run spends ~18 minutes running TFT inference + ARIMA
fitting for all 898 triplets. This is the same computation every time
unless new resistance data has been ingested. For development iteration
and demo scenarios, this is unacceptable.

THE SOLUTION
------------
Cache the forecast results to disk after the first run. On subsequent
runs, load from cache instead of recomputing. Cache is invalidated when:
    1. New resistance records have been ingested (ingestion_log timestamp)
    2. Cache is older than MAX_CACHE_AGE_HOURS (default: 48 hours)
    3. --force-refresh flag is passed to the orchestrator

CACHE FORMAT
------------
    data/cache/forecasts_{hash}.pkl
    data/cache/forecasts_meta.json

The hash is computed from:
    - Number of resistance records in the DB
    - Latest ingestion timestamp
    - Model checkpoint modification time

This ensures the cache is invalidated automatically when data changes.

USAGE IN ANOMALY DETECTOR
--------------------------
Replace:
    forecasts = generate_all_forecasts(model, train_df, reference_dataset)

With:
    from amr_sentinel.models.forecast_cache import get_or_compute_forecasts
    forecasts = get_or_compute_forecasts(model, train_df, reference_dataset)

EXPECTED SPEEDUP
----------------
    First run:      18-22 min (compute + cache write)
    Subsequent:     15-30 sec (cache load)
    Cache miss:     18-22 min (recompute + cache write)

Dependencies:
    pickle (stdlib), hashlib (stdlib), sqlalchemy (existing)
"""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CACHE_DIR = Path("amr_sentinel/data/cache")
CACHE_META_PATH = CACHE_DIR / "forecasts_meta.json"
MAX_CACHE_AGE_HOURS = 48
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Cache key computation
# ---------------------------------------------------------------------------

def _compute_cache_key() -> str:
    """
    Compute a cache key based on data state.

    Key inputs:
        - Total resistance records in DB
        - Latest ingestion timestamp
        - TFT checkpoint file modification time

    Returns a hex string that changes when any of these change.
    """
    from amr_sentinel.db.database import get_session
    from amr_sentinel.db.models import ResistanceRecord, IngestionLog
    from sqlalchemy import func

    hasher = hashlib.md5()

    try:
        with get_session() as session:
            # Record count
            count = session.query(func.count(ResistanceRecord.id)).scalar() or 0
            hasher.update(str(count).encode())

            # Latest ingestion timestamp
            latest = session.query(func.max(IngestionLog.completed_at)).scalar()
            if latest:
                hasher.update(str(latest).encode())

    except Exception as exc:
        logger.warning("Could not query DB for cache key: %s", exc)
        hasher.update(b"no_db")

    # TFT checkpoint modification time
    ckpt_path = Path("data/models/tft_amr_sentinel.ckpt")
    if ckpt_path.exists():
        hasher.update(str(ckpt_path.stat().st_mtime).encode())

    return hasher.hexdigest()[:16]


def _cache_path_for_key(key: str) -> Path:
    return CACHE_DIR / f"forecasts_{key}.pkl"


# ---------------------------------------------------------------------------
# Cache read/write
# ---------------------------------------------------------------------------

def _load_cache(key: str) -> dict | None:
    """
    Load cached forecasts if they exist and are fresh.

    Returns the forecasts dict, or None if cache miss/stale.
    """
    cache_path = _cache_path_for_key(key)

    if not cache_path.exists():
        logger.info("Forecast cache miss — no cache file for key %s", key)
        return None

    if not CACHE_META_PATH.exists():
        logger.info("Forecast cache miss — no metadata file")
        return None

    try:
        with open(CACHE_META_PATH) as f:
            meta = json.load(f)
    except Exception as exc:
        logger.warning("Could not read cache metadata: %s", exc)
        return None

    # Check age
    cached_at = meta.get("cached_at")
    if cached_at:
        age_hours = (time.time() - cached_at) / 3600
        if age_hours > MAX_CACHE_AGE_HOURS:
            logger.info(
                "Forecast cache stale (%.1f hours > %d hour limit)",
                age_hours, MAX_CACHE_AGE_HOURS,
            )
            return None

    # Check key matches
    if meta.get("cache_key") != key:
        logger.info(
            "Forecast cache invalidated — key mismatch (%s != %s)",
            meta.get("cache_key"), key,
        )
        return None

    try:
        logger.info("Loading forecasts from cache: %s", cache_path)
        t0 = time.time()
        with open(cache_path, "rb") as f:
            forecasts = pickle.load(f)
        elapsed = time.time() - t0
        logger.info(
            "Forecast cache hit — loaded %d triplets in %.1fs",
            len(forecasts), elapsed,
        )
        return forecasts

    except Exception as exc:
        logger.warning("Could not load forecast cache: %s", exc)
        return None


def _save_cache(key: str, forecasts: dict) -> None:
    """Save forecasts dict to cache."""
    cache_path = _cache_path_for_key(key)

    try:
        t0 = time.time()
        with open(cache_path, "wb") as f:
            pickle.dump(forecasts, f, protocol=pickle.HIGHEST_PROTOCOL)
        elapsed = time.time() - t0

        meta = {
            "cache_key": key,
            "cached_at": time.time(),
            "cached_at_iso": datetime.now(timezone.utc).isoformat(),
            "triplet_count": len(forecasts),
            "cache_file": str(cache_path),
            "write_elapsed_s": round(elapsed, 2),
        }
        with open(CACHE_META_PATH, "w") as f:
            json.dump(meta, f, indent=2)

        logger.info(
            "Forecast cache saved: %d triplets → %s (%.1fs)",
            len(forecasts), cache_path.name, elapsed,
        )

    except Exception as exc:
        logger.warning("Could not save forecast cache: %s", exc)


def clear_cache() -> None:
    """Delete all cached forecast files."""
    deleted = 0
    for pkl_file in CACHE_DIR.glob("forecasts_*.pkl"):
        pkl_file.unlink()
        deleted += 1
    if CACHE_META_PATH.exists():
        CACHE_META_PATH.unlink()
    logger.info("Forecast cache cleared. Deleted %d file(s).", deleted)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def get_or_compute_forecasts(
    model,
    train_df,
    reference_dataset,
    force_refresh: bool = False,
) -> dict:
    """
    Return forecasts from cache if available and fresh, otherwise compute.

    This is a drop-in replacement for the forecast generation loop in
    anomaly_detector.py. Same return type — dict keyed by triplet tuples.

    Args:
        model: Loaded TFT model.
        train_df: Prepared training DataFrame.
        reference_dataset: TimeSeriesDataSet reference.
        force_refresh: If True, bypass cache and recompute.

    Returns:
        Dict of {(pathogen, antibiotic, country): {year: forecast_dict}}
        with "_ci" key for confidence intervals.
    """
    if not force_refresh:
        key = _compute_cache_key()
        cached = _load_cache(key)
        if cached is not None:
            return cached
    else:
        key = _compute_cache_key()
        logger.info("Force refresh — bypassing forecast cache")

    # Cache miss — compute forecasts
    logger.info("Computing forecasts (cache miss)...")
    from amr_sentinel.models.anomaly_detector import generate_all_forecasts
    forecasts = generate_all_forecasts(model, train_df, reference_dataset)

    # Save to cache
    if forecasts:
        _save_cache(key, forecasts)

    return forecasts


# ---------------------------------------------------------------------------
# Cache status report
# ---------------------------------------------------------------------------

def cache_status() -> dict:
    """Return current cache status as a dict."""
    if not CACHE_META_PATH.exists():
        return {"status": "empty", "message": "No forecast cache found."}

    try:
        with open(CACHE_META_PATH) as f:
            meta = json.load(f)
    except Exception:
        return {"status": "error", "message": "Could not read cache metadata."}

    age_hours = (time.time() - meta.get("cached_at", 0)) / 3600
    current_key = _compute_cache_key()
    key_match = meta.get("cache_key") == current_key

    return {
        "status": "valid" if (key_match and age_hours <= MAX_CACHE_AGE_HOURS) else "stale",
        "cache_key": meta.get("cache_key"),
        "current_key": current_key,
        "key_match": key_match,
        "cached_at": meta.get("cached_at_iso"),
        "age_hours": round(age_hours, 1),
        "max_age_hours": MAX_CACHE_AGE_HOURS,
        "triplet_count": meta.get("triplet_count"),
        "cache_file": meta.get("cache_file"),
    }


# ---------------------------------------------------------------------------
# Standalone usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    if "--clear" in sys.argv:
        clear_cache()
        print("Cache cleared.")
        sys.exit(0)

    if "--status" in sys.argv:
        status = cache_status()
        print(json.dumps(status, indent=2))
        sys.exit(0)

    print("Forecast cache utility")
    print("  python -m amr_sentinel.models.forecast_cache --status")
    print("  python -m amr_sentinel.models.forecast_cache --clear")