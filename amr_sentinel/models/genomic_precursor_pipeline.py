"""
Genomic Precursor Pipeline
===========================
Runs the genomic precursor detector and converts PrecursorSignal objects
into alert-compatible dicts that are appended to output_queue.jsonl.
Called by the orchestrator as Stage 3b, after phenotypic triage and before
stewardship — so genomic precursor alerts get LLM bulletins and PubMed
citations alongside phenotypic alerts.

Can also be run standalone:
    python -m amr_sentinel.models.genomic_precursor_pipeline --dry-run
    python -m amr_sentinel.models.genomic_precursor_pipeline --high-confidence-only

Signal type: 'genomic_precursor'
Only HIGH and MEDIUM confidence signals flow to the pipeline.
LOW confidence (surveillance gap countries like USA, CHN) are skipped.

Inputs:
    - genomic_signals table (NCBI NDARO)
    - resistance_records table (phenotypic context)
    - data/alerts/output_queue.jsonl (appended to, not overwritten)

Outputs:
    - Appended rows in output_queue.jsonl
    - Summary dict returned to orchestrator

Dependencies: same as genomic_precursor_detector.py + uuid + hashlib

Idempotency note (fixed 2026-07-06):
    alert_id is now a deterministic UUID derived from the signal's logical
    identity (gene + pathogen + country + year) rather than a random UUID.
    Previously, every daily pipeline run generated a brand-new random UUID
    for the same underlying genomic signal, so alert_writer.py's UUID-based
    deduplication never recognised repeat signals as duplicates — causing
    the same signal to be re-inserted as a "new" alert on every single run
    (observed: one signal inserted 40 times over ~40 daily runs). Making
    the ID deterministic means the same signal always maps to the same
    alert_id across runs, so alert_writer.py correctly skips it as an
    existing row instead of duplicating it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("genomic_precursor_pipeline")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_QUEUE = PROJECT_ROOT / "data" / "alerts" / "output_queue.jsonl"

# Score thresholds — calibrated to genomic precursor max score (~80)
CRITICAL_THRESHOLD = 72
WARN_THRESHOLD = 50
MIN_SCORE = 50

# Only these confidence levels flow into the alert pipeline
PIPELINE_CONFIDENCE_LEVELS = {"HIGH", "MEDIUM"}


def _deterministic_alert_id(signal) -> str:
    """
    Build a stable UUID from a genomic signal's logical identity.

    The same underlying signal (same gene, pathogen, country, and latest
    data year) must always produce the same alert_id across pipeline runs.
    This allows alert_writer.py's UUID-based deduplication to correctly
    skip re-insertion of a signal that was already written on a previous
    run, instead of treating a random new UUID as a brand-new alert every
    single day.

    Args:
        signal: PrecursorSignal dataclass instance. Must expose
            gene_name, pathogen_name, country_iso3, and latest_year.

    Returns:
        A UUID string, deterministically derived via MD5 of the signal's
        identity key. Not cryptographically secure — not needed here,
        this is purely for stable deduplication, not security.
    """
    key = (
        f"genomic|{signal.gene_name}|{signal.pathogen_name}|"
        f"{signal.country_iso3}|{signal.latest_year}"
    )
    return str(uuid.UUID(hashlib.md5(key.encode("utf-8")).hexdigest()))


def precursor_to_alert_dict(signal, cycle_id: str = "") -> dict:
    """
    Convert a PrecursorSignal to an output_queue-compatible alert dict.

    Maps genomic-specific fields to the Alert schema. Fields that don't
    apply to genomic precursors (forecasted_rate, deviation_magnitude) use
    proxies or None.

    Args:
        signal: PrecursorSignal dataclass instance
        cycle_id: Pipeline cycle ID for traceability

    Returns:
        Dict ready to be written as a line in output_queue.jsonl
    """
    now = datetime.now(timezone.utc).isoformat()

    if signal.severity_score >= CRITICAL_THRESHOLD:
        tier = "critical"
    elif signal.severity_score >= WARN_THRESHOLD:
        tier = "warn"
    else:
        tier = "monitor"

    # current_resistance: phenotypic rate if known, else 0.0
    # The absence IS the signal — 0.0 with a growing gene count is the story
    current_resistance = (
        signal.phenotypic_rate if signal.phenotypic_rate is not None else 0.0
    )

    # deviation_magnitude: proxy — normalised isolate count (0.0–1.0)
    deviation_magnitude = min(1.0, signal.latest_isolate_count / 1000.0)

    time_series_str = ", ".join(
        f"{y}: {c} isolates" for y, c in sorted(signal.time_series.items())
    )

    return {
        # Core alert schema fields
        "alert_id": _deterministic_alert_id(signal),
        "created_at": now,
        "pathogen_name": signal.pathogen_name,
        "antibiotic_name": signal.drug_class,   # gene confers class-level resistance
        "country_iso3": signal.country_iso3,
        "severity_score": signal.severity_score,
        "severity_tier": tier,
        "signal_type": "genomic_precursor",
        "current_resistance": current_resistance,
        "forecasted_rate": None,
        "deviation_magnitude": deviation_magnitude,
        "trend_direction": "rising",
        "evidence_years": sorted(signal.time_series.keys()),
        "antibiotic_class": signal.drug_class,
        "who_region": signal.region_who or "",
        "stewardship_guidance": None,           # filled by stewardship agent
        "evidence_citations": [],               # filled by evidence linker
        "routing_target": "surveillance",
        "feedback_score": None,
        "feedback_note": None,
        "cycle_id": cycle_id,
        # Genomic-specific extended fields (used by dashboard and stewardship agent)
        "gene_name": signal.gene_name,
        "gene_family": signal.gene_family,
        "gene_description": signal.gene_description,
        "isolate_count": signal.latest_isolate_count,
        "latest_year": signal.latest_year,
        "time_series": signal.time_series,
        "time_series_summary": time_series_str,
        "acceleration_score": signal.acceleration_score,
        "doubling_time_years": signal.doubling_time_years,
        "days_to_threshold": signal.days_to_threshold,
        "phenotypic_gap": signal.phenotypic_gap,
        "surveillance_confidence": signal.surveillance_confidence,
        "surveillance_caveat": signal.surveillance_caveat,
        "spread_risk_countries": signal.spread_risk_countries,
        "intelligence_summary": signal.intelligence_summary,
        "who_priority": signal.who_priority,
    }


def run_genomic_precursor_pipeline(
    min_score: int = MIN_SCORE,
    append_to_queue: bool = True,
    high_confidence_only: bool = False,
    cycle_id: str = "",
) -> dict:
    """
    Run the genomic precursor detector and append results to output_queue.jsonl.

    Called by the orchestrator as Stage 3b. Non-fatal — if it fails, the
    phenotypic pipeline results are unaffected.

    Args:
        min_score: Minimum signal score to include
        append_to_queue: If False, detect and report but do not write
        high_confidence_only: If True, only queue HIGH confidence signals
        cycle_id: Pipeline cycle ID for traceability

    Returns:
        Summary dict: total_signals_detected, queued_to_pipeline, critical, warn,
        skipped_low_confidence, output_queue, run_at
    """
    logger.info("=== Genomic Precursor Pipeline starting ===")

    from amr_sentinel.models.genomic_precursor_detector import run_detector

    all_signals = run_detector(min_score=min_score)

    allowed_confidence = {"HIGH"} if high_confidence_only else PIPELINE_CONFIDENCE_LEVELS
    queued_signals = [
        s for s in all_signals if s.surveillance_confidence in allowed_confidence
    ]
    skipped_low = len(all_signals) - len(queued_signals)

    logger.info(
        "Signals: %d total | %d queued (%s) | %d LOW confidence skipped",
        len(all_signals),
        len(queued_signals),
        "/".join(sorted(allowed_confidence)),
        skipped_low,
    )

    if not queued_signals:
        logger.info("No genomic precursor signals to queue")
        return {
            "total_signals_detected": len(all_signals),
            "queued_to_pipeline": 0,
            "critical": 0,
            "warn": 0,
            "skipped_low_confidence": skipped_low,
            "output_queue": str(OUTPUT_QUEUE),
            "run_at": datetime.now(timezone.utc).isoformat(),
        }

    alert_dicts = [precursor_to_alert_dict(s, cycle_id=cycle_id) for s in queued_signals]
    critical_count = sum(1 for a in alert_dicts if a["severity_tier"] == "critical")
    warn_count = sum(1 for a in alert_dicts if a["severity_tier"] == "warn")

    if append_to_queue:
        OUTPUT_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_QUEUE, "a", encoding="utf-8") as f:
            for alert in alert_dicts:
                f.write(json.dumps(alert, default=str) + "\n")
        logger.info(
            "Appended %d genomic precursor alerts (%d critical, %d warn) to %s",
            len(alert_dicts), critical_count, warn_count, OUTPUT_QUEUE,
        )
    else:
        logger.info(
            "DRY RUN: would append %d alerts (%d critical, %d warn)",
            len(alert_dicts), critical_count, warn_count,
        )

    # Log top signals
    logger.info("Top genomic precursor signals queued:")
    for a in alert_dicts[:8]:
        logger.info(
            "  [%d/%s] %s / %s / %s — %s n=%d conf=%s",
            a["severity_score"], a["severity_tier"],
            a["pathogen_name"], a["antibiotic_name"], a["country_iso3"],
            a["gene_name"], a["isolate_count"], a["surveillance_confidence"],
        )

    return {
        "total_signals_detected": len(all_signals),
        "queued_to_pipeline": len(alert_dicts),
        "critical": critical_count,
        "warn": warn_count,
        "skipped_low_confidence": skipped_low,
        "output_queue": str(OUTPUT_QUEUE),
        "run_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run genomic precursor detector and append to alert pipeline"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect signals but do not write to output queue")
    parser.add_argument("--min-score", type=int, default=MIN_SCORE,
                        help=f"Minimum score threshold (default: {MIN_SCORE})")
    parser.add_argument("--high-confidence-only", action="store_true",
                        help="Only queue HIGH confidence signals")
    args = parser.parse_args()

    summary = run_genomic_precursor_pipeline(
        min_score=args.min_score,
        append_to_queue=not args.dry_run,
        high_confidence_only=args.high_confidence_only,
    )

    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")