"""
AMR-Sentinel — Alert Writer
=============================
Reads enriched alerts from output_queue.jsonl and writes them to the
PostgreSQL alerts table.

Genomic precursor alerts carry extra fields (gene_name, isolate_count,
doubling_time_years, etc.) that don't exist as typed columns in the alerts
table. These are packed into the extra_data JSONB column on insert and
unpacked by the API on read.

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
        "alert_id", "id", "created_at", "cycle_id",
    }
    for k, v in row.items():
        if k not in known and v is not None:
            extra[k] = v

    return extra


def _parse_alert_row(row: dict[str, Any]) -> Alert:
    """
    Convert a raw JSONL alert dict (from output_queue.jsonl) into an Alert ORM object.

    Genomic precursor fields are packed into extra_data JSONB.

    Args:
        row: Parsed JSON object from output_queue.jsonl

    Returns:
        Alert: Populated but not yet committed ORM instance
    """
    raw_id = row.get("alert_id") or row.get("id")
    try:
        alert_id = uuid.UUID(str(raw_id)) if raw_id else uuid.uuid4()
    except (ValueError, AttributeError):
        alert_id = uuid.uuid4()

    extra_data = _build_extra_data(row)

    return Alert(
        id=alert_id,
        pipeline_run_id=row.get("pipeline_run_id") or row.get("cycle_id"),
        pathogen_name=row.get("pathogen_name", "Unknown"),
        antibiotic_name=row.get("antibiotic_name", "Unknown"),
        antibiotic_class=row.get("antibiotic_class"),
        country_iso3=row.get("country_iso3", "UNK"),
        region_who=row.get("region_who"),
        severity_score=int(row.get("severity_score", 0)),
        severity_tier=row.get("severity_tier", "monitor"),
        signal_type=row.get("signal_type", "trajectory_deviation"),
        current_resistance=row.get("current_resistance"),
        forecasted_rate=row.get("forecasted_rate"),
        deviation_magnitude=row.get("deviation_magnitude"),
        trend_direction=row.get("trend_direction"),
        data_year=row.get("data_year") or row.get("year"),
        stewardship_guidance=row.get("stewardship_guidance"),
        evidence_citations=row.get("evidence_citations"),
        routing_target=row.get("routing_target"),
        forecast_lower_80=row.get("forecast_lower_80"),
        forecast_upper_80=row.get("forecast_upper_80"),
        forecast_lower_50=row.get("forecast_lower_50"),
        forecast_upper_50=row.get("forecast_upper_50"),
        extra_data=extra_data if extra_data else None,
    )


def write_alerts_from_queue(
    queue_file: Path = DEFAULT_QUEUE,
    skip_existing: bool = True,
) -> dict[str, int]:
    """
    Read output_queue.jsonl and write each alert to the PostgreSQL alerts table.

    Args:
        queue_file: Path to the JSONL alert queue file
        skip_existing: If True, skip alerts whose UUID already exists (idempotent)

    Returns:
        dict: Counts of inserted, skipped, and errored alerts

    Raises:
        FileNotFoundError: If queue_file does not exist
    """
    if not queue_file.exists():
        raise FileNotFoundError(f"Queue file not found: {queue_file}")

    lines = queue_file.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        logger.info("Queue file is empty — nothing to write.")
        return {"inserted": 0, "skipped": 0, "errored": 0}

    logger.info("Reading %d alerts from %s", len(lines), queue_file)

    inserted = skipped = errored = 0
    genomic_count = 0

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
                alert = _parse_alert_row(row)

                if alert.id in existing_ids:
                    logger.debug("Alert %s already exists — skipping.", alert.id)
                    skipped += 1
                    continue

                session.add(alert)
                inserted += 1
                if row.get("signal_type") == "genomic_precursor":
                    genomic_count += 1

            except json.JSONDecodeError as exc:
                logger.error("Line %d: JSON parse error — %s", i, exc)
                errored += 1
            except Exception as exc:
                logger.error("Line %d: Failed to write alert — %s", i, exc)
                errored += 1

    logger.info(
        "Alert writer complete. Inserted: %d (%d genomic) | Skipped: %d | Errored: %d",
        inserted, genomic_count, skipped, errored,
    )
    return {"inserted": inserted, "skipped": skipped, "errored": errored}


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
        help="Raise on duplicate UUIDs instead of skipping (default: skip)",
    )
    args = parser.parse_args()

    try:
        result = write_alerts_from_queue(
            queue_file=args.queue_file,
            skip_existing=not args.no_skip,
        )
        print(
            f"\nDone. Inserted: {result['inserted']} | "
            f"Skipped: {result['skipped']} | "
            f"Errored: {result['errored']}"
        )
        sys.exit(0 if result["errored"] == 0 else 1)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)