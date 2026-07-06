"""
amr_sentinel/ingestion/glass_ingestor.py
========================================
WHO GLASS AMR data ingestor via the GHO OData API.

What it does:
    - Pulls all GLASS AMR resistance rate indicators from the WHO Global Health
      Observatory (GHO) OData API — no authentication required
    - Covers all WHO Bacterial Priority Pathogens List (BPPL) 2024 pathogens
      available via GLASS: Critical, High, and Medium priority tiers
    - Normalises each record into the unified AMR-Sentinel resistance schema
    - Writes to PostgreSQL idempotently (safe to re-run)
    - Logs ingestion statistics per run to the ingestion_log table

API base: https://ghoapi.azureedge.net/api/

WHO BPPL 2024 Critical (tracked): E. coli/3GC, S. aureus/MRSA,
    K. pneumoniae/Carbapenem, K. pneumoniae/3GC, Acinetobacter/Carbapenem
WHO BPPL 2024 High (tracked): Salmonella Typhi/FQ, Shigella/FQ,
    N. gonorrhoeae/Ceftriaxone, N. gonorrhoeae/FQ
WHO BPPL 2024 Medium (tracked): S. pneumoniae/Penicillin,
    S. pneumoniae/Macrolide, H. influenzae/Ampicillin

Coverage: 100+ countries, 2016-2024.
Not yet in GLASS (M. tuberculosis, C. auris) handled by separate ingestors.

Inputs:
    - WHO GHO OData API (public, no authentication required)
    - PostgreSQL connection via environment variables in .env

Outputs:
    - Populated resistance_records table in amr_sentinel PostgreSQL database
    - Ingestion run summary in ingestion_log table
    - Log file at logs/glass_ingestor.log

External dependencies:
    pip install requests sqlalchemy psycopg2-binary python-dotenv tqdm
"""

import os
import logging
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, text, Table, Column, MetaData,
    String, Integer, Float, DateTime, UniqueConstraint
)
from sqlalchemy.exc import SQLAlchemyError
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "glass_ingestor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("glass_ingestor")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

load_dotenv()

DATABASE_URL = (
    f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)

GHO_API_BASE = "https://ghoapi.azureedge.net/api"

# ---------------------------------------------------------------------------
# GLASS indicators available via the GHO API
# Each entry maps: indicator_code -> (pathogen_name, antibiotic_name, antibiotic_class)
# Extend this dict as WHO publishes additional GLASS indicators via the API
# ---------------------------------------------------------------------------

GLASS_INDICATORS: dict[str, tuple[str, str, str]] = {
    # WHO GHO OData API only publishes two GLASS AMR indicators as of 2026.
    # All other WHO priority pathogens are in GLASS reports but not yet exposed
    # as machine-readable GHO API endpoints. Those pathogens are covered by the
    # ECDC EARS-Net ingestor (EU/EEA) and Nigeria PubMed miner (Africa).
    # Monitor https://ghoapi.azureedge.net/api/Indicator for new releases.
    "AMR_INFECT_ECOLI": (
        "Escherichia coli",
        "Ceftriaxone",           # 3rd-gen cephalosporin representative
        "Cephalosporins (3rd gen)",
    ),
    "AMR_INFECT_MRSA": (
        "Staphylococcus aureus",
        "Methicillin",
        "Penicillins",
    ),
}

# WHO region code → full name
WHO_REGION_NAMES: dict[str, str] = {
    "AFR": "AFRO",
    "AMR": "AMRO",
    "EMR": "EMRO",
    "EUR": "EURO",
    "SEAR": "SEARO",
    "WPR": "WPRO",
}

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def get_engine():
    """Create and return a SQLAlchemy engine.

    Returns:
        sqlalchemy.engine.Engine

    Raises:
        SQLAlchemyError: If the connection cannot be established.
    """
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def ensure_tables(engine) -> None:
    """Create resistance_records and ingestion_log tables if they do not exist.

    Uses IF NOT EXISTS semantics via SQLAlchemy metadata — safe to call
    on every run.

    Args:
        engine: SQLAlchemy engine connected to the target database.
    """
    metadata = MetaData()

    Table(
        "resistance_records",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("pathogen_name", String, nullable=False),
        Column("pathogen_ncbi_id", String, nullable=True),
        Column("antibiotic_name", String, nullable=False),
        Column("antibiotic_class", String, nullable=True),
        Column("country_iso3", String(3), nullable=False),
        Column("region_who", String, nullable=True),
        Column("year", Integer, nullable=False),
        Column("quarter", Integer, nullable=True),
        Column("resistance_rate", Float, nullable=True),
        Column("sample_count", Integer, nullable=True),
        Column("data_source", String, nullable=False),
        Column("source_record_id", String, nullable=True),
        Column("ingested_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint(
            "pathogen_name",
            "antibiotic_name",
            "country_iso3",
            "year",
            "quarter",
            "data_source",
            name="uq_resistance_record",
        ),
    )

    Table(
        "ingestion_log",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("source", String, nullable=False),
        Column("file_path", String, nullable=True),
        Column("started_at", DateTime(timezone=True), nullable=False),
        Column("completed_at", DateTime(timezone=True), nullable=True),
        Column("rows_processed", Integer, nullable=True),
        Column("rows_inserted", Integer, nullable=True),
        Column("rows_skipped", Integer, nullable=True),
        Column("rows_errored", Integer, nullable=True),
        Column("status", String, nullable=False),
        Column("notes", String, nullable=True),
    )

    metadata.create_all(engine)
    logger.info("Database tables verified / created.")


# ---------------------------------------------------------------------------
# GHO API client
# ---------------------------------------------------------------------------

def fetch_indicator(
    indicator_code: str,
    retries: int = 3,
    backoff: float = 2.0,
) -> list[dict]:
    """Fetch all records for a single GHO indicator via the OData API.

    Handles pagination automatically — the GHO API returns results in
    pages of 1000 records. Continues fetching until all pages are
    retrieved or a non-recoverable error occurs.

    Args:
        indicator_code: GHO indicator code, e.g. "AMR_INFECT_ECOLI".
        retries: Number of retry attempts on transient HTTP errors.
        backoff: Base backoff time in seconds between retries (doubles
                 each attempt).

    Returns:
        List of raw record dicts as returned by the GHO API.

    Raises:
        RuntimeError: If all retry attempts are exhausted.
    """
    url = f"{GHO_API_BASE}/{indicator_code}"
    all_records: list[dict] = []
    page = 0
    page_size = 1000

    while True:
        paginated_url = f"{url}?$skip={page * page_size}&$top={page_size}"
        attempt = 0

        while attempt < retries:
            try:
                response = requests.get(paginated_url, timeout=30)
                response.raise_for_status()
                data = response.json()
                records = data.get("value", [])
                all_records.extend(records)
                logger.debug(
                    "Fetched page %d for %s — %d records",
                    page, indicator_code, len(records),
                )
                # If fewer records than page_size returned we are on the last page
                if len(records) < page_size:
                    return all_records
                page += 1
                break

            except requests.exceptions.HTTPError as exc:
                if response.status_code == 404:
                    logger.error(
                        "Indicator %s not found in GHO API (404).", indicator_code
                    )
                    return all_records
                logger.warning(
                    "HTTP error fetching %s page %d (attempt %d/%d): %s",
                    indicator_code, page, attempt + 1, retries, exc,
                )
                attempt += 1
                time.sleep(backoff * attempt)

            except requests.exceptions.RequestException as exc:
                logger.warning(
                    "Request error fetching %s page %d (attempt %d/%d): %s",
                    indicator_code, page, attempt + 1, retries, exc,
                )
                attempt += 1
                time.sleep(backoff * attempt)

        else:
            raise RuntimeError(
                f"Failed to fetch {indicator_code} page {page} "
                f"after {retries} attempts."
            )


# ---------------------------------------------------------------------------
# Record normalisation
# ---------------------------------------------------------------------------

def normalise_record(
    raw: dict,
    indicator_code: str,
    pathogen_name: str,
    antibiotic_name: str,
    antibiotic_class: str,
    ingested_at: datetime,
) -> Optional[dict]:
    """Normalise a single GHO API record into the unified resistance schema.

    Args:
        raw: Raw record dict from the GHO API response.
        indicator_code: GHO indicator code for this record.
        pathogen_name: Canonical pathogen name for this indicator.
        antibiotic_name: Canonical antibiotic name for this indicator.
        antibiotic_class: Antibiotic class for this indicator.
        ingested_at: Timestamp of this ingestion run.

    Returns:
        Normalised record dict ready for database insertion, or None if
        the record is invalid and should be skipped.
    """
    country_iso3 = raw.get("SpatialDim")
    year = raw.get("TimeDim")
    numeric_value = raw.get("NumericValue")
    who_region_raw = raw.get("ParentLocationCode", "")
    gho_id = raw.get("Id")

    # Skip records missing critical fields
    if not country_iso3 or not year:
        return None

    # Skip non-country spatial dimensions (e.g. regional aggregates)
    if raw.get("SpatialDimType") != "COUNTRY":
        return None

    # Normalise resistance rate from percentage to proportion (0.0–1.0)
    resistance_rate: Optional[float] = None
    if numeric_value is not None:
        try:
            rate = float(numeric_value)
            # GHO stores as percentage (e.g. 81.63), convert to proportion
            resistance_rate = round(rate / 100.0, 6)
            if not 0.0 <= resistance_rate <= 1.0:
                resistance_rate = None
        except (ValueError, TypeError):
            resistance_rate = None

    region_who = WHO_REGION_NAMES.get(who_region_raw, who_region_raw or None)

    source_record_id = f"GHO|{indicator_code}|{gho_id}"

    return {
        "pathogen_name": pathogen_name,
        "pathogen_ncbi_id": None,
        "antibiotic_name": antibiotic_name,
        "antibiotic_class": antibiotic_class,
        "country_iso3": str(country_iso3).strip().upper(),
        "region_who": region_who,
        "year": int(year),
        "quarter": None,
        "resistance_rate": resistance_rate,
        "sample_count": None,
        "data_source": "GLASS",
        "source_record_id": source_record_id,
        "ingested_at": ingested_at,
    }


# ---------------------------------------------------------------------------
# Database writer
# ---------------------------------------------------------------------------

INSERT_SQL = text("""
    INSERT INTO resistance_records (
        pathogen_name, pathogen_ncbi_id, antibiotic_name, antibiotic_class,
        country_iso3, region_who, year, quarter, resistance_rate,
        sample_count, data_source, source_record_id, ingested_at
    ) VALUES (
        :pathogen_name, :pathogen_ncbi_id, :antibiotic_name, :antibiotic_class,
        :country_iso3, :region_who, :year, :quarter, :resistance_rate,
        :sample_count, :data_source, :source_record_id, :ingested_at
    )
    ON CONFLICT ON CONSTRAINT uq_resistance_record DO NOTHING
""")


def write_records(
    records: list[dict],
    engine,
) -> dict[str, int]:
    """Write normalised resistance records to PostgreSQL.

    Uses INSERT ... ON CONFLICT DO NOTHING for full idempotency.

    Args:
        records: List of normalised record dicts.
        engine: SQLAlchemy engine.

    Returns:
        Stats dict: rows_processed, rows_inserted, rows_skipped, rows_errored.
    """
    stats = {
        "rows_processed": len(records),
        "rows_inserted": 0,
        "rows_skipped": 0,
        "rows_errored": 0,
    }

    with engine.begin() as conn:
        for record in tqdm(records, desc="Writing to database", unit="row"):
            try:
                result = conn.execute(INSERT_SQL, record)
                if result.rowcount == 1:
                    stats["rows_inserted"] += 1
                else:
                    stats["rows_skipped"] += 1
            except SQLAlchemyError as exc:
                logger.error("DB error on record %s: %s", record.get("source_record_id"), exc)
                stats["rows_errored"] += 1

    return stats


# ---------------------------------------------------------------------------
# Ingestion log
# ---------------------------------------------------------------------------

def log_run(
    engine,
    started_at: datetime,
    stats: dict[str, int],
    status: str,
    notes: Optional[str] = None,
) -> None:
    """Write a run summary row to ingestion_log.

    Args:
        engine: SQLAlchemy engine.
        started_at: When the run began.
        stats: Ingestion statistics dict.
        status: "success" or "failed".
        notes: Optional error message or annotation.
    """
    sql = text("""
        INSERT INTO ingestion_log (
            source, file_path, started_at, completed_at,
            rows_processed, rows_inserted, rows_skipped, rows_errored,
            status, notes
        ) VALUES (
            :source, :file_path, :started_at, :completed_at,
            :rows_processed, :rows_inserted, :rows_skipped, :rows_errored,
            :status, :notes
        )
    """)
    with engine.begin() as conn:
        conn.execute(sql, {
            "source": "GLASS_GHO_API",
            "file_path": None,
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc),
            "rows_processed": stats.get("rows_processed", 0),
            "rows_inserted": stats.get("rows_inserted", 0),
            "rows_skipped": stats.get("rows_skipped", 0),
            "rows_errored": stats.get("rows_errored", 0),
            "status": status,
            "notes": notes,
        })


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_glass_ingestor() -> dict[str, int]:
    """Run the full GLASS ingestion pipeline via the WHO GHO OData API.

    Fetches all configured GLASS AMR indicators, normalises each record,
    writes to PostgreSQL idempotently, and logs the run.

    Returns:
        Aggregated stats dict across all indicators:
        {rows_processed, rows_inserted, rows_skipped, rows_errored}
    """
    started_at = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("GLASS GHO API ingestor started: %s", started_at.isoformat())
    logger.info("Indicators to fetch: %s", list(GLASS_INDICATORS.keys()))

    engine = get_engine()
    ensure_tables(engine)

    total_stats = {
        "rows_processed": 0,
        "rows_inserted": 0,
        "rows_skipped": 0,
        "rows_errored": 0,
    }
    status = "failed"
    ingested_at = datetime.now(timezone.utc)

    try:
        for indicator_code, (pathogen, antibiotic, ab_class) in GLASS_INDICATORS.items():
            logger.info(
                "Fetching indicator: %s (%s / %s)",
                indicator_code, pathogen, antibiotic,
            )

            raw_records = fetch_indicator(indicator_code)
            logger.info(
                "Fetched %d raw records for %s", len(raw_records), indicator_code
            )

            normalised = []
            for raw in raw_records:
                record = normalise_record(
                    raw, indicator_code, pathogen,
                    antibiotic, ab_class, ingested_at,
                )
                if record:
                    normalised.append(record)

            skipped_normalisation = len(raw_records) - len(normalised)
            if skipped_normalisation > 0:
                logger.warning(
                    "Skipped %d records during normalisation (missing country/year).",
                    skipped_normalisation,
                )

            stats = write_records(normalised, engine)

            # Accumulate totals
            for key in total_stats:
                total_stats[key] += stats.get(key, 0)
            total_stats["rows_skipped"] += skipped_normalisation

        status = "success"

    except RuntimeError as exc:
        logger.error("API fetch failed: %s", exc)
        total_stats["notes"] = str(exc)
    except SQLAlchemyError as exc:
        logger.error("Database error: %s", exc)
        total_stats["notes"] = str(exc)
    finally:
        log_run(
            engine,
            started_at,
            total_stats,
            status,
            notes=total_stats.get("notes"),
        )

    logger.info("-" * 60)
    logger.info("GLASS ingestion complete. Status: %s", status.upper())
    logger.info("  Rows processed : %d", total_stats["rows_processed"])
    logger.info("  Rows inserted  : %d", total_stats["rows_inserted"])
    logger.info("  Rows skipped   : %d", total_stats["rows_skipped"])
    logger.info("  Rows errored   : %d", total_stats["rows_errored"])
    logger.info("=" * 60)

    return total_stats


# ---------------------------------------------------------------------------
# Standalone usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    result = run_glass_ingestor()
    sys.exit(0 if result.get("rows_errored", 0) == 0 else 1)