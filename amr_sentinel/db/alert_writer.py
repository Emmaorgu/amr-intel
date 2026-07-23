"""
AMR-Sentinel — Alert Writer
=============================
Reads enriched alerts from output_queue.jsonl and writes them to the
PostgreSQL alerts table.

Idempotency strategy (updated):
- Phenotypic alerts: UPSERT — insert on first detection, then UPDATE
  updated_at + severity_score on every subsequent pipeline run.
  created_at is NEVER overwritten — it is the immutable lead-time anchor.
  updated_at = "last confirmed active by pipeline" — what time filters
  (Yesterday, This Week) compare against.
- Genomic precursor alerts: UPSERT — insert on first run, update
  severity/extra_data + updated_at on subsequent runs.

This means every daily pipeline run that confirms a signal is still
active stamps that alert with today's updated_at — preserving the
continuous surveillance record as the proprietary moat.

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

TYPED_COLUMNS = {
    "pipeline_run_id", "pathogen_name", "antibiotic_name", "antibiotic_class",
    "country_iso3", "region_who", "severity_score", "severity_tier",
    "signal_type", "current_resistance", "forecasted_rate", "deviation_magnitude",
    "trend_direction", "data_year", "stewardship_guidance", "evidence_citations",
    "routing_target", "forecast_lower_80", "forecast_upper_80",
    "forecast_lower_50", "forecast_upper_50",
}

GENOMIC_EXTRA_FIELDS = {
    "gene_name", "gene_family", "gene_description",
    "isolate_count", "latest_year", "time_series", "time_series_summary",
    "acceleration_score", "doubling_time_years", "days_to_threshold",
    "phenotypic_gap", "surveillance_confidence", "surveillance_caveat",
    "spread_risk_countries", "intelligence_summary", "who_priority",
    "genomic_context", "evidence_years", "precursor_tier",
}


def _build_extra_data(row: dict[str, Any]) -> dict[str, Any]:
    """
    Extract genomic-specific fields from a row dict for the extra_data JSONB column.

    Args:
        row: Raw parsed JSONL row

    Returns:
        Dict of extra fields (empty for phenotypic alerts)
    """
    extra = {}
    for field in GENOMIC_EXTRA_FIELDS:
        if field in row and row[field] is not None:
            extra[field] = row[field]

    known = TYPED_COLUMNS | GENOMIC_EXTRA_FIELDS | {
        "alert_id", "id", "created_at", "updated_at", "cycle_id",
        "signal_id", "detected_at",
    }
    for k, v in row.items():
        if k not in known and v is not None:
            extra[k] = v

    return extra


def _parse_alert_row(row: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a raw JSONL alert dict into column values for SQLAlchemy upsert.

    Args:
        row: Parsed JSON object from output_queue.jsonl

    Returns:
        Dict of column name -> value
    """
    signal_type = row.get("signal_type", "trajectory_deviation")

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
) -> dict[str, int]:
    """
    Read output_queue.jsonl and upsert each alert to the PostgreSQL alerts table.

    On first detection: INSERT with created_at = now().
    On every subsequent pipeline run: UPDATE updated_at = now() (and refresh
    severity_score, severity_tier, current_resistance, trend_direction,
    stewardship_guidance, evidence_citations, extra_data).

    created_at is NEVER overwritten — it is the immutable lead-time anchor
    recording exactly when AMR-Intel first detected this signal.

    updated_at records the last pipeline confirmation. Time window filters
    in the dashboard compare against updated_at, so "Yesterday" always shows
    every signal the pipeline confirmed active yesterday.

    Args:
        queue_file: Path to the JSONL alert queue file

    Returns:
        dict: Counts of inserted, updated, and errored alerts

    Raises:
        FileNotFoundError: If queue_file does not exist
    """
    if not queue_file.exists():
        raise FileNotFoundError("Queue file not found: " + str(queue_file))

    lines = queue_file.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        logger.info("Queue file is empty — nothing to write.")
        return {"inserted": 0, "updated": 0, "errored": 0}

    logger.info("Reading %d alerts from %s", len(lines), queue_file)

    inserted = updated = errored = 0

    with get_session() as session:
        # Track existing IDs to distinguish insert vs update in logging
        existing_ids: set[uuid.UUID] = {
            row[0] for row in session.query(Alert.id).all()
        }

        for i, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                values = _parse_alert_row(row)
                alert_id = values["id"]

                # Fields updated on every pipeline confirmation run.
                # Refreshes intelligence while preserving created_at (lead-time anchor).
                update_fields = {
                    "updated_at":          "now()",   # marks "confirmed active today"
                    "severity_score":      values["severity_score"],
                    "severity_tier":       values["severity_tier"],
                    "current_resistance":  values["current_resistance"],
                    "trend_direction":     values["trend_direction"],
                    "pipeline_run_id":     values["pipeline_run_id"],
                }

                # For genomic precursor alerts also refresh intelligence payload
                signal_type = row.get("signal_type", "trajectory_deviation")
                if signal_type == "genomic_precursor":
                    update_fields["extra_data"] = values["extra_data"]

                # For all alerts, refresh bulletin if pipeline regenerated it
                if values.get("stewardship_guidance"):
                    update_fields["stewardship_guidance"] = values["stewardship_guidance"]
                if values.get("evidence_citations"):
                    update_fields["evidence_citations"] = values["evidence_citations"]

                stmt = (
                    pg_insert(Alert)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["id"],
                        set_=update_fields,
                    )
                )
                session.execute(stmt)

                if alert_id in existing_ids:
                    updated += 1
                    logger.debug("Alert %s confirmed active — updated_at refreshed.", alert_id)
                else:
                    inserted += 1
                    logger.debug("Alert %s inserted (new signal).", alert_id)

            except json.JSONDecodeError as exc:
                logger.error("Line %d: JSON parse error — %s", i, exc)
                errored += 1
            except Exception as exc:
                logger.error("Line %d: Failed to write alert — %s", i, exc, exc_info=True)
                errored += 1

    logger.info(
        "Alert writer complete. Inserted (new): %d | Updated (confirmed active): %d | Errored: %d",
        inserted, updated, errored,
    )
    return {"inserted": inserted, "updated": updated, "errored": errored}


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Write/upsert alerts from output_queue.jsonl into PostgreSQL."
    )
    parser.add_argument(
        "--queue-file", type=Path, default=DEFAULT_QUEUE,
        help="Path to output_queue.jsonl (default: data/alerts/output_queue.jsonl)",
    )
    args = parser.parse_args()

    try:
        result = write_alerts_from_queue(queue_file=args.queue_file)
        print(
            "\nDone. Inserted (new): " + str(result["inserted"]) +
            " | Updated (confirmed active): " + str(result["updated"]) +
            " | Errored: " + str(result["errored"])
        )
        sys.exit(0 if result["errored"] == 0 else 1)
    except FileNotFoundError as e:
        print("Error: " + str(e))
        sys.exit(1)