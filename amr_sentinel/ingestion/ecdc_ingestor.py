"""
amr_sentinel/ingestion/ecdc_ingestor.py
========================================
ECDC EARS-Net AMR data ingestor via the ECDC Surveillance Atlas REST API.

What it does:
    - Fetches resistance rate data for 6 WHO-priority pathogens across
      11 antibiotic classes from the ECDC Atlas API (2014–2024)
    - Covers 30 EU/EEA countries with annual data — ~25,000+ records
    - Normalises each record into the unified AMR-Sentinel resistance schema
    - Writes to PostgreSQL idempotently (safe to re-run)
    - Logs each run to the ingestion_log table

This is the primary training data source for the v1 trajectory forecaster.
Combined with the GHO GLASS data (Task 1.1), it provides sufficient
coverage for the Temporal Fusion Transformer model.

API base: https://atlas.ecdc.europa.eu/public/AtlasService/rest/
Key endpoint: GetMeasureResultsForTimePeriodAndGeoLevel

Pathogens covered:
    ACISPP  — Acinetobacter spp.
    ENCFAE  — Enterococcus faecalis
    ENCFAI  — Enterococcus faecium
    ESCCOL  — Escherichia coli
    KLEPNE  — Klebsiella pneumoniae
    PSEAER  — Pseudomonas aeruginosa
    STAAUR  — Staphylococcus aureus (MRSA)
    STRPNE  — Streptococcus pneumoniae

Inputs:
    - ECDC Atlas REST API (public, no authentication required)
    - PostgreSQL connection via environment variables in .env

Outputs:
    - Populated resistance_records table in amr_sentinel PostgreSQL database
    - Ingestion run summary in ingestion_log table
    - Log file at logs/ecdc_ingestor.log

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
from sqlalchemy import create_engine, text
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
        logging.FileHandler(LOG_DIR / "ecdc_ingestor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("ecdc_ingestor")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

load_dotenv()

DATABASE_URL = (
    f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)

ECDC_API_BASE = "https://atlas.ecdc.europa.eu/public/AtlasService/rest"
ECDC_HEALTH_TOPIC_ID = 4   # Antimicrobial resistance
ECDC_GEO_LEVEL = 2         # Country level

# ---------------------------------------------------------------------------
# Pathogen + antibiotic measure mapping
#
# Maps ECDC measure code suffix → (pathogen_name, antibiotic_name, class)
# We only pull R.PROPORTION measures (resistance percentage).
# COMPLETENESS and COMBINED measures are excluded.
# ---------------------------------------------------------------------------

MEASURE_MAP: dict[str, tuple[str, str, str]] = {
    # Acinetobacter spp.
    "ACISPP.AMINOGLYCOSIDES.R.PROPORTION": (
        "Acinetobacter spp.", "Gentamicin", "Aminoglycosides"),
    "ACISPP.CARBAPENEMS.R.PROPORTION": (
        "Acinetobacter spp.", "Imipenem", "Carbapenems"),
    "ACISPP.FLUOROQUINOLONES.R.PROPORTION": (
        "Acinetobacter spp.", "Ciprofloxacin", "Fluoroquinolones"),

    # Enterococcus faecalis
    "ENCFAE.AMINOPENICILLINS.R.PROPORTION": (
        "Enterococcus faecalis", "Ampicillin", "Penicillins"),
    "ENCFAE.GENTAHIGH.R.PROPORTION": (
        "Enterococcus faecalis", "Gentamicin (high level)", "Aminoglycosides"),
    "ENCFAE.VANCOMYCIN.R.PROPORTION": (
        "Enterococcus faecalis", "Vancomycin", "Glycopeptides"),

    # Enterococcus faecium
    "ENCFAI.AMINOPENICILLINS.R.PROPORTION": (
        "Enterococcus faecium", "Ampicillin", "Penicillins"),
    "ENCFAI.GENTAHIGH.R.PROPORTION": (
        "Enterococcus faecium", "Gentamicin (high level)", "Aminoglycosides"),
    "ENCFAI.VANCOMYCIN.R.PROPORTION": (
        "Enterococcus faecium", "Vancomycin", "Glycopeptides"),

    # Escherichia coli
    "ESCCOL.AMINOGLYCOSIDES.R.PROPORTION": (
        "Escherichia coli", "Gentamicin", "Aminoglycosides"),
    "ESCCOL.AMINOPENICILLINS.R.PROPORTION": (
        "Escherichia coli", "Ampicillin", "Penicillins"),
    "ESCCOL.CARBAPENEMS.R.PROPORTION": (
        "Escherichia coli", "Imipenem", "Carbapenems"),
    "ESCCOL.CEF.R.PROPORTION": (
        "Escherichia coli", "Ceftriaxone", "Cephalosporins (3rd gen)"),
    "ESCCOL.FLUOROQUINOLONES.R.PROPORTION": (
        "Escherichia coli", "Ciprofloxacin", "Fluoroquinolones"),

    # Klebsiella pneumoniae
    "KLEPNE.AMINOGLYCOSIDES.R.PROPORTION": (
        "Klebsiella pneumoniae", "Gentamicin", "Aminoglycosides"),
    "KLEPNE.CARBAPENEMS.R.PROPORTION": (
        "Klebsiella pneumoniae", "Imipenem", "Carbapenems"),
    "KLEPNE.CEF.R.PROPORTION": (
        "Klebsiella pneumoniae", "Ceftriaxone", "Cephalosporins (3rd gen)"),
    "KLEPNE.FLUOROQUINOLONES.R.PROPORTION": (
        "Klebsiella pneumoniae", "Ciprofloxacin", "Fluoroquinolones"),

    # Pseudomonas aeruginosa
    "PSEAER.AMINOGLYCOSIDES.R.PROPORTION": (
        "Pseudomonas aeruginosa", "Gentamicin", "Aminoglycosides"),
    "PSEAER.CARBAPENEMS.R.PROPORTION": (
        "Pseudomonas aeruginosa", "Imipenem", "Carbapenems"),
    "PSEAER.CAZ.R.PROPORTION": (
        "Pseudomonas aeruginosa", "Ceftazidime", "Cephalosporins (3rd gen)"),
    "PSEAER.FLUOROQUINOLONES.R.PROPORTION": (
        "Pseudomonas aeruginosa", "Ciprofloxacin", "Fluoroquinolones"),
    "PSEAER.UREIDOPEN.R.PROPORTION": (
        "Pseudomonas aeruginosa", "Piperacillin-tazobactam",
        "Beta-lactam combinations"),

    # Staphylococcus aureus
    "STAAUR.OXA.R.PROPORTION": (
        "Staphylococcus aureus", "Oxacillin", "Penicillins"),

    # Streptococcus pneumoniae — expand beyond macrolides
    "STRPNE.MACROLIDES.R.PROPORTION": (
        "Streptococcus pneumoniae", "Azithromycin", "Macrolides"),
    "STRPNE.PENICILLINS.R.PROPORTION": (
        "Streptococcus pneumoniae", "Penicillin", "Penicillins"),
    "STRPNE.CEF.R.PROPORTION": (
        "Streptococcus pneumoniae", "Ceftriaxone", "Cephalosporins (3rd gen)"),

    # Haemophilus influenzae (WHO BPPL 2024 Medium — new)
    "HAEINF.AMINOPENICILLINS.R.PROPORTION": (
        "Haemophilus influenzae", "Ampicillin", "Penicillins"),
    "HAEINF.CEF.R.PROPORTION": (
        "Haemophilus influenzae", "Ceftriaxone", "Cephalosporins (3rd gen)"),

    # Salmonella spp. — fluoroquinolone resistance (WHO BPPL 2024 High — new)
    # ECDC tracks Salmonella from EFSA/ECDC One Health reports
    "SALSPP.FLUOROQUINOLONES.R.PROPORTION": (
        "Salmonella spp.", "Ciprofloxacin", "Fluoroquinolones"),
    "SALSPP.CEF.R.PROPORTION": (
        "Salmonella spp.", "Ceftriaxone", "Cephalosporins (3rd gen)"),

    # Shigella spp. — fluoroquinolone resistance (WHO BPPL 2024 High — new)
    "SHISPP.FLUOROQUINOLONES.R.PROPORTION": (
        "Shigella spp.", "Ciprofloxacin", "Fluoroquinolones"),

    # Neisseria gonorrhoeae (WHO BPPL 2024 High — new)
    # ECDC tracks via EURO-GASP (European Gonococcal Antimicrobial Surveillance Programme)
    "NEIGON.CEF.R.PROPORTION": (
        "Neisseria gonorrhoeae", "Ceftriaxone", "Cephalosporins (3rd gen)"),
    "NEIGON.FLUOROQUINOLONES.R.PROPORTION": (
        "Neisseria gonorrhoeae", "Ciprofloxacin", "Fluoroquinolones"),
    "NEIGON.AZITHROMYCIN.R.PROPORTION": (
        "Neisseria gonorrhoeae", "Azithromycin", "Macrolides"),
}

# ISO 3166-1 alpha-2 → alpha-3 mapping for ECDC country codes
ISO2_TO_ISO3: dict[str, str] = {
    "AT": "AUT", "BE": "BEL", "BG": "BGR", "CY": "CYP", "CZ": "CZE",
    "DE": "DEU", "DK": "DNK", "EE": "EST", "EL": "GRC", "ES": "ESP",
    "FI": "FIN", "FR": "FRA", "HR": "HRV", "HU": "HUN", "IE": "IRL",
    "IS": "ISL", "IT": "ITA", "LI": "LIE", "LT": "LTU", "LU": "LUX",
    "LV": "LVA", "MT": "MLT", "NL": "NLD", "NO": "NOR", "PL": "POL",
    "PT": "PRT", "RO": "ROU", "SE": "SWE", "SI": "SVN", "SK": "SVK",
    # Non-EU EARS-Net participants
    "GB": "GBR", "ME": "MNE", "MK": "MKD", "RS": "SRB", "TR": "TUR",
    "UA": "UKR", "AL": "ALB", "AM": "ARM", "AZ": "AZE", "BY": "BLR",
    "GE": "GEO", "KZ": "KAZ", "KG": "KGZ", "MD": "MDA", "TJ": "TJK",
    "TM": "TKM", "UZ": "UZB",
}

# ---------------------------------------------------------------------------
# ECDC Atlas API client
# ---------------------------------------------------------------------------

def get_measure_ids_for_dataset(dataset_id: int) -> dict[str, int]:
    """Fetch all measure IDs for a given ECDC dataset.

    Queries the Atlas API for all indicator measures in the AMR health
    topic for the specified dataset, then filters to only the R.PROPORTION
    measures defined in MEASURE_MAP.

    Args:
        dataset_id: ECDC dataset ID (e.g. 1881 for 2023.AMR.YEARLY.V1).

    Returns:
        Dict mapping measure_code → measure_id for all codes in MEASURE_MAP
        that are available in this dataset.
    """
    url = (
        f"{ECDC_API_BASE}/GetIndicatorMeasuresForHealthTopicAndDataset"
        f"?datasetId={dataset_id}&healthTopicId={ECDC_HEALTH_TOPIC_ID}"
    )
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    data = response.json()

    measure_ids: dict[str, int] = {}
    for measure in data.get("Measures", []):
        code = measure.get("Code", "")
        if code in MEASURE_MAP:
            measure_ids[code] = measure["Id"]

    logger.debug(
        "Dataset %d: found %d matching measures out of %d total.",
        dataset_id, len(measure_ids), len(data.get("Measures", [])),
    )
    return measure_ids


def fetch_measure_results(
    measure_id: int,
    start_year: str,
    end_year_excl: str,
    retries: int = 3,
    backoff: float = 2.0,
) -> list[dict]:
    """Fetch country-level resistance results for a single measure.

    Uses GetMeasureResultsForTimePeriodAndGeoLevel which is fast and
    targeted — returns only the data for the specified measure and
    time range.

    Args:
        measure_id: ECDC internal measure ID.
        start_year: Start year string e.g. "2014".
        end_year_excl: Exclusive end year string e.g. "2025".
        retries: Number of retry attempts on failure.
        backoff: Base backoff seconds between retries.

    Returns:
        List of raw MeasureResult dicts from the API response.

    Raises:
        RuntimeError: If all retry attempts are exhausted.
    """
    url = (
        f"{ECDC_API_BASE}/GetMeasureResultsForTimePeriodAndGeoLevel"
        f"?measureIds={measure_id}"
        f"&startTimeCode={start_year}"
        f"&endTimeCodeExcl={end_year_excl}"
        f"&geoLevel={ECDC_GEO_LEVEL}"
    )

    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            return data.get("MeasureResults", [])

        except requests.exceptions.RequestException as exc:
            logger.warning(
                "Request failed for measure %d (attempt %d/%d): %s",
                measure_id, attempt, retries, exc,
            )
            if attempt < retries:
                time.sleep(backoff * attempt)

    raise RuntimeError(
        f"Failed to fetch measure {measure_id} after {retries} attempts."
    )


# ---------------------------------------------------------------------------
# Record normalisation
# ---------------------------------------------------------------------------

def normalise_record(
    raw: dict,
    measure_code: str,
    pathogen_name: str,
    antibiotic_name: str,
    antibiotic_class: str,
    ingested_at: datetime,
) -> Optional[dict]:
    """Normalise a single ECDC API result into the unified resistance schema.

    Args:
        raw: Raw MeasureResult dict from the ECDC API.
        measure_code: ECDC measure code e.g. "ESCCOL.FLUOROQUINOLONES.R.PROPORTION".
        pathogen_name: Canonical pathogen name.
        antibiotic_name: Canonical antibiotic name.
        antibiotic_class: Antibiotic class.
        ingested_at: Timestamp of this ingestion run.

    Returns:
        Normalised record dict ready for database insertion, or None if
        the record is invalid and should be skipped.
    """
    iso2 = raw.get("GeoCountry") or raw.get("dGeoMnemonic")
    time_code = raw.get("TimeCode")
    y_value = raw.get("YValue")
    n_tested = raw.get("N")
    uid = raw.get("UID")

    if not iso2 or not time_code:
        return None

    # Convert ISO2 → ISO3
    iso3 = ISO2_TO_ISO3.get(str(iso2).upper())
    if not iso3:
        logger.debug("Unknown ISO2 country code: %s — skipping.", iso2)
        return None

    # Parse year
    try:
        year = int(time_code)
    except (ValueError, TypeError):
        return None

    # Normalise resistance rate from percentage to proportion
    resistance_rate: Optional[float] = None
    if y_value is not None:
        try:
            rate = float(y_value) / 100.0
            if 0.0 <= rate <= 1.0:
                resistance_rate = round(rate, 6)
        except (ValueError, TypeError):
            pass

    # Parse sample count
    sample_count: Optional[int] = None
    if n_tested is not None:
        try:
            sample_count = int(n_tested)
        except (ValueError, TypeError):
            pass

    return {
        "pathogen_name": pathogen_name,
        "pathogen_ncbi_id": None,
        "antibiotic_name": antibiotic_name,
        "antibiotic_class": antibiotic_class,
        "country_iso3": iso3,
        "region_who": "EURO",
        "year": year,
        "quarter": None,
        "resistance_rate": resistance_rate,
        "sample_count": sample_count,
        "data_source": "ECDC",
        "source_record_id": f"ECDC|{measure_code}|{iso2}|{time_code}|{uid}",
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


def write_records(records: list[dict], engine) -> dict[str, int]:
    """Write normalised records to PostgreSQL idempotently.

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

    for record in records:
        try:
            with engine.begin() as conn:
                result = conn.execute(INSERT_SQL, record)
                if result.rowcount == 1:
                    stats["rows_inserted"] += 1
                else:
                    stats["rows_skipped"] += 1
        except SQLAlchemyError as exc:
            logger.error(
                "DB error on %s: %s",
                record.get("source_record_id"), exc,
            )
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
    """Write a run summary to ingestion_log.

    Args:
        engine: SQLAlchemy engine.
        started_at: When the run began.
        stats: Ingestion statistics.
        status: "success" or "failed".
        notes: Optional error message.
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
            "source": "ECDC_ATLAS_API",
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

def run_ecdc_ingestor() -> dict[str, int]:
    """Run the full ECDC EARS-Net ingestion pipeline.

    Strategy:
        1. Fetch all AMR datasets available (one per year 2014–2024)
        2. For each dataset, skip any whose code cannot yield a 4-digit
           year prefix (e.g. CURRENT.AMR.YEARLY) — fix for the
           "invalid literal for int()" crash on non-yearly datasets
        3. For each dataset, look up measure IDs for all codes in MEASURE_MAP
        4. For each measure, fetch country-level resistance results
        5. Normalise and write to PostgreSQL idempotently

    Returns:
        Aggregated stats dict across all measures and datasets.
    """
    started_at = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("ECDC EARS-Net ingestor started: %s", started_at.isoformat())

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    # Fetch all AMR datasets
    url = (
        f"{ECDC_API_BASE}/GetDatasetsForHealthTopic"
        f"?healthTopicId={ECDC_HEALTH_TOPIC_ID}"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    all_datasets = r.json().get("Datasets", [])

    # Keep only the latest version per year — skip CURRENT and GENERAL
    year_to_dataset: dict[str, dict] = {}
    for ds in all_datasets:
        code = ds["Code"]

        # --- FIX: skip any dataset whose first 4 characters are not a year ---
        try:
            year_int = int(code[:4])
        except ValueError:
            logger.info(
                "Skipping non-yearly dataset: %s (cannot parse year from code)",
                code,
            )
            continue
        # ---------------------------------------------------------------------

        year_prefix = str(year_int)
        if (year_prefix not in year_to_dataset
                or ds["Id"] > year_to_dataset[year_prefix]["Id"]):
            year_to_dataset[year_prefix] = ds

    datasets_to_process = sorted(
        year_to_dataset.values(), key=lambda d: d["Code"]
    )
    logger.info(
        "Found %d yearly AMR datasets to process: %s",
        len(datasets_to_process),
        [d["Code"] for d in datasets_to_process],
    )

    total_stats: dict[str, int] = {
        "rows_processed": 0,
        "rows_inserted": 0,
        "rows_skipped": 0,
        "rows_errored": 0,
    }
    status = "failed"
    ingested_at = datetime.now(timezone.utc)

    try:
        for dataset in datasets_to_process:
            dataset_id = dataset["Id"]
            dataset_code = dataset["Code"]

            # Parse year safely — already validated above but be explicit
            try:
                year_int = int(dataset_code[:4])
            except ValueError:
                logger.info(
                    "Skipping non-yearly dataset in loop: %s", dataset_code
                )
                continue

            start_year = str(year_int)
            end_year_excl = str(year_int + 1)

            logger.info(
                "Processing dataset: %s (ID %d)", dataset_code, dataset_id
            )

            # Get measure IDs for this dataset
            try:
                measure_ids = get_measure_ids_for_dataset(dataset_id)
            except requests.exceptions.RequestException as exc:
                logger.error(
                    "Failed to get measures for dataset %s: %s",
                    dataset_code, exc,
                )
                continue

            if not measure_ids:
                logger.warning(
                    "No matching measures found in dataset %s — skipping.",
                    dataset_code,
                )
                continue

            logger.info(
                "  Found %d measures to fetch for %s",
                len(measure_ids), dataset_code,
            )

            all_records: list[dict] = []

            for measure_code, measure_id in tqdm(
                measure_ids.items(),
                desc=f"  {dataset_code}",
                unit="measure",
            ):
                pathogen, antibiotic, ab_class = MEASURE_MAP[measure_code]

                try:
                    raw_results = fetch_measure_results(
                        measure_id,
                        start_year=start_year,
                        end_year_excl=end_year_excl,
                    )
                except RuntimeError as exc:
                    logger.error(
                        "Failed to fetch measure %s (%d): %s",
                        measure_code, measure_id, exc,
                    )
                    continue

                for raw in raw_results:
                    record = normalise_record(
                        raw, measure_code, pathogen,
                        antibiotic, ab_class, ingested_at,
                    )
                    if record:
                        all_records.append(record)

            if all_records:
                stats = write_records(all_records, engine)
                for key in total_stats:
                    total_stats[key] += stats.get(key, 0)
                logger.info(
                    "  %s: inserted %d / skipped %d / errored %d",
                    dataset_code,
                    stats["rows_inserted"],
                    stats["rows_skipped"],
                    stats["rows_errored"],
                )

        status = "success"

    except Exception as exc:
        logger.error("Unexpected error: %s", exc, exc_info=True)
        total_stats["notes"] = str(exc)
    finally:
        log_run(
            engine, started_at, total_stats, status,
            notes=total_stats.get("notes"),
        )

    logger.info("-" * 60)
    logger.info("ECDC ingestion complete. Status: %s", status.upper())
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
    result = run_ecdc_ingestor()
    sys.exit(0 if result.get("rows_errored", 0) == 0 else 1)