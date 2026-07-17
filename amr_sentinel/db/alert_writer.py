"""
AMR-Sentinel — Alert Writer
=============================
Reads enriched alerts from output_queue.jsonl and writes them to the
PostgreSQL alerts table.

Genomic precursor alerts carry extra fields (gene_name, isolate_count,
doubling_time_years, etc.) that don't exist as typed columns in the alerts
table. These are packed into the extra_data JSONB column on insert and
unpacked by the API on read.

Idempotency strategy:
- Phenotypic alerts (trajectory_deviation, rate_spike): skip if UUID exists.
  The triage agent already generates deterministic UUIDs for these.
- Genomic precursor alerts: UPSERT — insert on first run, update severity/
  extra_data on subsequent runs. The detector generates a deterministic UUID
  from (gene_name, pathogen_name, country_iso3) so the same signal always
  maps to the same DB row regardless of how many times the pipeline fires.

Usage:
    python -m amr_sentinel.db.alert_writer
    python -m amr_sentinel.db.alert_writer --queue-file path/to/output_queue.jsonl

Dependencies:
    sqlalchemy, psycopg2-binary, python-dotenv
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from amr_sentinel.db.database import get_session
from amr_sentinel.db.models import Alert

logger = logging.getLogger(__name__)

DEFAULT_QUEUE = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "alerts"
    / "output_queue.jsonl"
)

# Fields that live in named columns on the alerts table
TYPED_COLUMNS = {
    "pipeline_run_id", "pathogen_name", "antibiotic_name", "antibiotic_class",
    "country_iso3", "region_who", "severity_score", "severity_tier",
    "signal_type", "current_resistance", "forecasted_rate", "deviation_magnitude",
    "trend_direction", "data_year", "stewardship_guidance", "evidence_citations",
    "routing_target", "forecast_lower_80", "forecast_upper_80",
    "forecast_lower_50", "forecast_upper_50",
}

# Genomic-specific fields that go into extra_data JSONB
GENOMIC_EXTRA_FIELDS = {
    "gene_name", "gene_family", "gene_description",
    "isolate_count", "latest_year", "time_series", "time_series_summary",
    "acceleration_score", "doubling_time_years", "days_to_threshold",
    "phenotypic_gap", "surveillance_confidence", "surveillance_caveat",
    "spread_risk_countries", "intelligence_summary", "who_priority",
    "genomic_context", "evidence_years",
}


def _build_extra_data(row: dict[str, Any]) -> dict[str, Any]:
    """
    Extract genomic-specific fields from a row dict and return them as
    a dict suitable for storage in the extra_data JSONB column.

    For phenotypic alerts this will return an empty dict.
    For genomic precursor alerts this preserves all the intelligence context
    that the dashboard and API need to render the signal correctly.

    Args:
        row: Raw parsed JSONL row

    Returns:
        Dict of extra fields (may be empty for phenotypic alerts)
    """
    extra = {}
    for field in GENOMIC_EXTRA_FIELDS:
        if field in row and row[field] is not None:
            extra[field] = row[field]

    # Also capture any non-null fields not in either set (future-proofing)
    known = TYPED_COLUMNS | GENOMIC_EXTRA_FIELDS | {
        "alert_id", "id", "created_at", "cycle_id", "signal_id", "detected_at",
    }
    for k, v in row.items():
        if k not in known and v is not None:
            extra[k] = v

    return extra


def _parse_alert_row(row: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a raw JSONL alert dict into a flat dict of column values
    suitable for SQLAlchemy insert/upsert.

    For genomic precursor alerts, signal_id (the deterministic UUID from the
    detector) is preferred as the primary key over alert_id/id, so the same
    gene/pathogen/country always maps to the same DB row.

    Args:
        row: Parsed JSON object from output_queue.jsonl

    Returns:
        Dict of column name -> value (not yet committed)
    """
    signal_type = row.get("signal_type", "trajectory_deviation")

    # UUID resolution: genomic precursor alerts carry signal_id (deterministic).
    # Phenotypic alerts carry alert_id from the triage agent (also deterministic).
    if signal_type == "genomic_precursor" and row.get("signal_id"):
        try:
            alert_id = uuid.UUID(str(row["signal_id"]))
        except (ValueError, AttributeError):
            alert_id = uuid.uuid4()
    else:
        raw_id = row.get("alert_id") or row.get("id")
        try:
            alert_id = uuid.UUID(str(raw_id)) if raw_id else uuid.uuid4()
        except (ValueError, AttributeError):
            alert_id = uuid.uuid4()

    extra_data = _build_extra_data(row)

    return {
        "id": alert_id,
        "pipeline_run_id": row.get("pipeline_run_id") or row.get("cycle_id"),
        "pathogen_name": row.get("pathogen_name", "Unknown"),
        "antibiotic_name": row.get("antibiotic_name", "Unknown"),
        "antibiotic_class": row.get("antibiotic_class"),
        "country_iso3": row.get("country_iso3", "UNK"),
        "region_who": row.get("region_who"),
        "severity_score": int(row.get("severity_score", 0)),
        "severity_tier": row.get("severity_tier", "monitor"),
        "signal_type": signal_type,
        "current_resistance": row.get("current_resistance"),
        "forecasted_rate": row.get("forecasted_rate"),
        "deviation_magnitude": row.get("deviation_magnitude"),
        "trend_direction": row.get("trend_direction"),
        "data_year": row.get("data_year") or row.get("year"),
        "stewardship_guidance": row.get("stewardship_guidance"),
        "evidence_citations": row.get("evidence_citations"),
        "routing_target": row.get("routing_target"),
        "forecast_lower_80": row.get("forecast_lower_80"),
        "forecast_upper_80": row.get("forecast_upper_80"),
        "forecast_lower_50": row.get("forecast_lower_50"),
        "forecast_upper_50": row.get("forecast_upper_50"),
        "extra_data": extra_data if extra_data else None,
    }


def write_alerts_from_queue(
    queue_file: Path = DEFAULT_QUEUE,
    skip_existing: bool = True,
) -> dict[str, int]:
    """
    Read output_queue.jsonl and write each alert to the PostgreSQL alerts table.

    Phenotypic alerts (trajectory_deviation, rate_spike) are skipped if their
    UUID already exists in the DB — they are immutable once written.

    Genomic precursor alerts are UPSERTED — inserted on first run, and on
    subsequent runs the severity_score, severity_tier, and extra_data are
    updated in-place. This means re-running the pipeline refreshes genomic
    signal intelligence without accumulating duplicate rows.

    Args:
        queue_file: Path to the JSONL alert queue file
        skip_existing: If True, skip phenotypic alerts whose UUID already exists

    Returns:
        dict: Counts of inserted, upserted, skipped, and errored alerts

    Raises:
        FileNotFoundError: If queue_file does not exist
    """
    if not queue_file.exists():
        raise FileNotFoundError("Queue file not found: " + str(queue_file))

    lines = queue_file.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        logger.info("Queue file is empty — nothing to write.")
        return {"inserted": 0, "upserted": 0, "skipped": 0, "errored": 0}

    logger.info("Reading %d alerts from %s", len(lines), queue_file)

    inserted = upserted = skipped = errored = 0

    with get_session() as session:
        if skip_existing:
            existing_ids: set[uuid.UUID] = {
                row[0] for row in session.query(Alert.id).all()
            }
        else:
            existing_ids = set()

        for i, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                signal_type = row.get("signal_type", "trajectory_deviation")
                values = _parse_alert_row(row)

                if signal_type == "genomic_precursor":
                    # UPSERT: on conflict (same deterministic UUID), update the
                    # mutable intelligence fields. created_at is excluded so the
                    # original detection timestamp is preserved.
                    stmt = (
                        pg_insert(Alert)
                        .values(**values)
                        .on_conflict_do_update(
                            index_elements=["id"],
                            set_={
                                "severity_score": values["severity_score"],
                                "severity_tier": values["severity_tier"],
                                "extra_data": values["extra_data"],
                                "pipeline_run_id": values["pipeline_run_id"],
                            },
                        )
                    )
                    result = session.execute(stmt)
                    # rowcount=1 on insert, 0 on no-op update (unchanged row),
                    # but pg_insert always returns 1 for DO UPDATE — distinguish
                    # via whether ID was already in existing_ids.
                    if values["id"] in existing_ids:
                        upserted += 1
                        logger.debug("Genomic alert %s upserted (updated).", values["id"])
                    else:
                        inserted += 1
                        logger.debug("Genomic alert %s inserted (new).", values["id"])

                else:
                    # Phenotypic alerts: skip if already in DB (immutable)
                    if values["id"] in existing_ids:
                        logger.debug("Alert %s already exists — skipping.", values["id"])
                        skipped += 1
                        continue

                    alert_obj = Alert(**values)
                    session.add(alert_obj)
                    inserted += 1

            except json.JSONDecodeError as exc:
                logger.error("Line %d: JSON parse error — %s", i, exc)
                errored += 1
            except Exception as exc:
                logger.error("Line %d: Failed to write alert — %s", i, exc, exc_info=True)
                errored += 1

    logger.info(
        "Alert writer complete. Inserted: %d | Upserted (refreshed): %d | "
        "Skipped: %d | Errored: %d",
        inserted, upserted, skipped, errored,
    )
    return {"inserted": inserted, "upserted": upserted, "skipped": skipped, "errored": errored}


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Write alerts from output_queue.jsonl into PostgreSQL."
    )
    parser.add_argument(
        "--queue-file", type=Path, default=DEFAULT_QUEUE,
        help="Path to output_queue.jsonl (default: data/alerts/output_queue.jsonl)",
    )
    parser.add_argument(
        "--no-skip", action="store_true",
        help="Force re-insert phenotypic alerts (genomic always upserts regardless)",
    )
    args = parser.parse_args()

    try:
        result = write_alerts_from_queue(
            queue_file=args.queue_file,
            skip_existing=not args.no_skip,
        )
        print(
            "\nDone. Inserted: " + str(result["inserted"]) +
            " | Upserted: " + str(result["upserted"]) +
            " | Skipped: " + str(result["skipped"]) +
            " | Errored: " + str(result["errored"])
        )
        sys.exit(0 if result["errored"] == 0 else 1)
    except FileNotFoundError as e:
        print("Error: " + str(e))
        sys.exit(1)