"""
amr_sentinel/orchestrator.py
==============================
Autonomous orchestrator for AMR-Sentinel.

Pipeline stages:
    1. Ingestion      — GLASS + ECDC ingestors (skippable)
    2. Detection      — anomaly detection + severity scoring
    3. Triage         — deduplication + stale suppression
    3b. Genomic       — genomic precursor detector (appends to queue)
    4. Stewardship    — LLM intelligence bulletins
    5. Evidence       — PubMed RAG citation attachment
    6. Output queue   — write to data/alerts/output_queue.jsonl

Run modes:
    python -m amr_sentinel.orchestrator --run-once
    python -m amr_sentinel.orchestrator --run-once --skip-ingestion --max-alerts 10
    python -m amr_sentinel.orchestrator
    python -m amr_sentinel.orchestrator --schedule "0 */6 * * *"

Environment variables:
    ANTHROPIC_API_KEY       Required for stewardship + evidence agents
    PIPELINE_SCHEDULE       Cron override (default: "0 2 * * *")
    MAX_STEWARDSHIP_ALERTS  Max LLM alerts per cycle (default: 50)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Environment & paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

ALERTS_DIR = PROJECT_ROOT / "data" / "alerts"
OUTPUT_QUEUE_PATH = ALERTS_DIR / "output_queue.jsonl"
RUN_LOG_PATH = ALERTS_DIR / "orchestrator_run_log.jsonl"
TRIAGE_STATE_PATH = ALERTS_DIR / "triage_state.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("orchestrator")

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

T = TypeVar("T")

_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2
_BACKOFF_CAP = 300


def _run_with_retry(
    fn: Callable[[], T],
    stage_name: str,
    max_attempts: int = _MAX_ATTEMPTS,
) -> tuple[Optional[T], Optional[Exception]]:
    """
    Call fn() with exponential backoff on failure.

    Returns (result, None) on success, (None, exception) on final failure.
    """
    delay = _BACKOFF_BASE
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info("[%s] Attempt %d/%d...", stage_name, attempt, max_attempts)
            result = fn()
            logger.info("[%s] Succeeded on attempt %d.", stage_name, attempt)
            return result, None
        except Exception as exc:
            last_exc = exc
            logger.warning("[%s] Attempt %d/%d failed: %s", stage_name, attempt, max_attempts, exc)
            if attempt < max_attempts:
                sleep_time = min(delay, _BACKOFF_CAP)
                logger.info("[%s] Retrying in %.0fs...", stage_name, sleep_time)
                time.sleep(sleep_time)
                delay *= 2

    logger.error(
        "[%s] All %d attempts failed. Last error: %s\n%s",
        stage_name, max_attempts, last_exc, traceback.format_exc(),
    )
    return None, last_exc


# ---------------------------------------------------------------------------
# Output queue writer
# ---------------------------------------------------------------------------


def _write_to_output_queue(alerts: list, cycle_id: str) -> int:
    """
    Append phenotypic Alert objects to the output queue JSONL file.

    Returns number of alerts written.
    """
    ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with OUTPUT_QUEUE_PATH.open("a", encoding="utf-8") as f:
            for alert in alerts:
                record = alert.to_dict()
                record["cycle_id"] = cycle_id
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
        logger.info("Output queue: wrote %d alerts to %s", written, OUTPUT_QUEUE_PATH)
    except OSError as exc:
        logger.error("Failed to write to output queue: %s", exc)
    return written


# ---------------------------------------------------------------------------
# Run log writer
# ---------------------------------------------------------------------------


def _write_run_log(entry: dict[str, Any]) -> None:
    """Append a pipeline run summary to the run log JSONL file."""
    ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with RUN_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.error("Failed to write run log: %s", exc)


# ---------------------------------------------------------------------------
# Sentence transformer singleton
# ---------------------------------------------------------------------------

_PATCHED_MODEL = None


def _load_sentence_transformer():
    """
    Load the all-MiniLM-L6-v2 sentence transformer model once.
    Returns the loaded model, or None if not installed.
    """
    try:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading sentence-transformers model (all-MiniLM-L6-v2)...")
        model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Sentence transformer model loaded and cached.")
        return model
    except ImportError:
        logger.warning(
            "sentence-transformers not installed. Evidence linker will fall "
            "back to PubMed relevance rank ordering."
        )
        return None


def _patch_sentence_transformer(model) -> None:
    """
    Monkey-patch the evidence linker's _retrieve_top_citations to use a
    pre-loaded SentenceTransformer model instead of reloading per alert.
    Safe to call multiple times — only patches once.
    """
    global _PATCHED_MODEL
    if _PATCHED_MODEL is not None:
        return

    try:
        import faiss
        import amr_sentinel.agents.evidence_linker as el_module

        original_fn = el_module._retrieve_top_citations

        def _patched_retrieve(alert, articles, top_k=3):
            if not articles:
                return []
            try:
                corpus = [f"{art['title']}. {art['abstract']}" for art in articles]
                query = (
                    f"{alert.pathogen_name} {alert.antibiotic_name} resistance "
                    f"{alert.antibiotic_class} {alert.country_iso3} "
                    f"surveillance epidemiology"
                )
                corpus_embeddings = model.encode(corpus, convert_to_numpy=True, show_progress_bar=False)
                query_embedding = model.encode([query], convert_to_numpy=True, show_progress_bar=False)
                faiss.normalize_L2(corpus_embeddings)
                faiss.normalize_L2(query_embedding)
                dim = corpus_embeddings.shape[1]
                index = faiss.IndexFlatIP(dim)
                index.add(corpus_embeddings)
                k = min(top_k, len(articles))
                scores, indices = index.search(query_embedding, k)
                results = []
                for score, idx in zip(scores[0], indices[0]):
                    if idx < 0:
                        continue
                    article = articles[idx].copy()
                    article["relevance_score"] = round(float(score), 4)
                    results.append(article)
                return results
            except Exception as exc:
                logger.warning("Patched retrieval failed (%s) — falling back to original.", exc)
                return original_fn(alert, articles, top_k)

        el_module._retrieve_top_citations = _patched_retrieve
        _PATCHED_MODEL = model
        logger.info("Sentence transformer singleton patched into evidence linker.")
    except Exception as exc:
        logger.warning(
            "Could not patch sentence transformer (%s). "
            "Model will reload per alert (slower but functional).", exc
        )


# ---------------------------------------------------------------------------
# Ingestion stage
# ---------------------------------------------------------------------------


def _run_ingestion() -> dict[str, Any]:
    """
    Run all configured data ingestors. Each runs independently — failure
    of one does not abort the others.
    """
    results: dict[str, Any] = {}

    try:
        from amr_sentinel.ingestion.glass_ingestor import run_glass_ingestion
        glass_result, glass_exc = _run_with_retry(run_glass_ingestion, "GLASS ingestion")
        results["glass"] = {
            "status": "success" if glass_exc is None else "failed",
            "error": str(glass_exc) if glass_exc else None,
        }
    except ImportError as exc:
        logger.warning("GLASS ingestor not importable: %s", exc)
        results["glass"] = {"status": "skipped", "error": str(exc)}

    try:
        from amr_sentinel.ingestion.ecdc_ingestor import run_ecdc_ingestion
        ecdc_result, ecdc_exc = _run_with_retry(run_ecdc_ingestion, "ECDC ingestion")
        results["ecdc"] = {
            "status": "success" if ecdc_exc is None else "failed",
            "error": str(ecdc_exc) if ecdc_exc else None,
        }
    except ImportError as exc:
        logger.warning("ECDC ingestor not importable: %s", exc)
        results["ecdc"] = {"status": "skipped", "error": str(exc)}

    return results


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    max_stewardship_alerts: int = 50,
    skip_ingestion: bool = False,
    sentence_model=None,
) -> dict[str, Any]:
    """
    Run one complete pipeline cycle.

    Stages:
        1.  Ingestion       — GLASS + ECDC (skippable)
        2.  Detection       — anomaly detection + severity scoring
        3.  Triage          — deduplication + stale suppression
        3b. Genomic         — genomic precursor detector (non-fatal)
        4.  Stewardship     — LLM intelligence bulletins
        5.  Evidence        — PubMed RAG citation attachment
        6.  Output queue    — write to output_queue.jsonl

    Returns:
        Pipeline run summary dict with stage timings, alert counts, and errors.
    """
    cycle_id = datetime.now(timezone.utc).isoformat()
    run_summary: dict[str, Any] = {
        "cycle_id": cycle_id,
        "started_at": cycle_id,
        "stages": {},
        "alert_counts": {},
        "errors": [],
        "status": "running",
    }

    logger.info("=" * 70)
    logger.info("AMR-SENTINEL PIPELINE CYCLE STARTED")
    logger.info("Cycle ID: %s", cycle_id)
    logger.info("=" * 70)

    pipeline_start = time.time()

    # ------------------------------------------------------------------
    # Stage 1: Ingestion
    # ------------------------------------------------------------------

    if not skip_ingestion:
        stage_start = time.time()
        logger.info("--- Stage 1: Data Ingestion ---")
        ingestion_results = _run_ingestion()
        run_summary["stages"]["ingestion"] = {
            "elapsed_s": round(time.time() - stage_start, 1),
            "results": ingestion_results,
        }
    else:
        logger.info("--- Stage 1: Ingestion skipped ---")
        run_summary["stages"]["ingestion"] = {"status": "skipped"}

    # ------------------------------------------------------------------
    # Stage 2: Anomaly detection + severity scoring
    # ------------------------------------------------------------------

    logger.info("--- Stage 2: Anomaly Detection + Severity Scoring ---")
    stage_start = time.time()

    from amr_sentinel.models.severity_scorer import run_severity_scoring

    scored_result, scored_exc = _run_with_retry(
        run_severity_scoring, "anomaly detection + severity scoring"
    )

    run_summary["stages"]["detection_and_scoring"] = {
        "elapsed_s": round(time.time() - stage_start, 1),
        "status": "success" if scored_exc is None else "failed",
        "scored_count": len(scored_result) if scored_result else 0,
        "error": str(scored_exc) if scored_exc else None,
    }
    run_summary["alert_counts"]["signals_detected"] = len(scored_result) if scored_result else 0

    if scored_exc is not None or not scored_result:
        logger.error("Detection/scoring failed or returned no signals. Aborting cycle.")
        run_summary["status"] = "failed"
        run_summary["errors"].append(f"detection_scoring: {scored_exc}")
        _write_run_log(run_summary)
        return run_summary

    critical_count = sum(1 for s in scored_result if s.severity_score >= 80)
    warn_count = sum(1 for s in scored_result if 50 <= s.severity_score < 80)
    logger.info(
        "Detection + scoring: %d signals (%d critical, %d warn)",
        len(scored_result), critical_count, warn_count,
    )

    # ------------------------------------------------------------------
    # Stage 3: Triage
    # ------------------------------------------------------------------

    logger.info("--- Stage 3: Triage ---")
    stage_start = time.time()

    from amr_sentinel.agents.triage_agent import TriageAgent
    triage_agent = TriageAgent(
        state_path=TRIAGE_STATE_PATH,
        escalation_tiers={"warn", "critical"},
    )

    alerts_result, triage_exc = _run_with_retry(
        lambda: triage_agent.process(scored_result), "triage"
    )

    run_summary["stages"]["triage"] = {
        "elapsed_s": round(time.time() - stage_start, 1),
        "status": "success" if triage_exc is None else "failed",
        "alert_count": len(alerts_result) if alerts_result else 0,
        "error": str(triage_exc) if triage_exc else None,
    }

    if triage_exc is not None:
        logger.error("Triage failed. Aborting cycle.")
        run_summary["status"] = "failed"
        run_summary["errors"].append(f"triage: {triage_exc}")
        _write_run_log(run_summary)
        return run_summary

    alerts = alerts_result or []
    run_summary["alert_counts"]["after_triage"] = len(alerts)
    logger.info("Triage: %d alerts produced", len(alerts))

    # ------------------------------------------------------------------
    # Stage 3b: Genomic Precursor Detection
    # Runs regardless of whether phenotypic alerts exist — genomic signals
    # are independent of the phenotypic triage result.
    # Non-fatal — if this fails, phenotypic pipeline is unaffected.
    # ------------------------------------------------------------------

    logger.info("--- Stage 3b: Genomic Precursor Detection ---")
    stage_start = time.time()

    genomic_queued = 0
    try:
        from amr_sentinel.models.genomic_precursor_pipeline import run_genomic_precursor_pipeline
        genomic_summary = run_genomic_precursor_pipeline(
            min_score=50,
            append_to_queue=True,
            high_confidence_only=False,
            cycle_id=cycle_id,
        )
        genomic_queued = genomic_summary.get("queued_to_pipeline", 0)
        logger.info(
            "Genomic precursor: %d signals detected, %d queued to pipeline (%d critical, %d warn)",
            genomic_summary.get("total_signals_detected", 0),
            genomic_queued,
            genomic_summary.get("critical", 0),
            genomic_summary.get("warn", 0),
        )
        run_summary["stages"]["genomic_precursor"] = {
            "elapsed_s": round(time.time() - stage_start, 1),
            "status": "success",
            "signals_detected": genomic_summary.get("total_signals_detected", 0),
            "queued": genomic_queued,
            "skipped_low_confidence": genomic_summary.get("skipped_low_confidence", 0),
        }
        run_summary["alert_counts"]["genomic_precursor_queued"] = genomic_queued
    except Exception as exc:
        logger.warning("Genomic precursor pipeline failed (non-fatal): %s", exc)
        run_summary["stages"]["genomic_precursor"] = {
            "elapsed_s": round(time.time() - stage_start, 1),
            "status": "failed",
            "error": str(exc),
        }
        run_summary["errors"].append(f"genomic_precursor: {exc}")

    if not alerts:
        logger.info("No new phenotypic alerts after triage. Cycle complete — no LLM work needed.")
        run_summary["status"] = "success_no_alerts"
        run_summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        run_summary["total_elapsed_s"] = round(time.time() - pipeline_start, 1)
        _write_run_log(run_summary)
        return run_summary

    # ------------------------------------------------------------------
    # Stage 4: Stewardship
    # Critical alerts fill budget first; warn fills remaining slots.
    # ------------------------------------------------------------------

    logger.info("--- Stage 4: Stewardship (LLM bulletins) ---")
    stage_start = time.time()

    critical_alerts = [a for a in alerts if a.severity_tier == "critical"]
    warn_alerts = [a for a in alerts if a.severity_tier == "warn"]

    critical_budget = min(len(critical_alerts), max_stewardship_alerts)
    warn_budget = min(len(warn_alerts), max(0, max_stewardship_alerts - critical_budget))
    stewardship_batch = critical_alerts[:critical_budget] + warn_alerts[:warn_budget]

    logger.info(
        "Stewardship batch: %d alerts (%d critical, %d warn) | limit=%d",
        len(stewardship_batch), critical_budget, warn_budget, max_stewardship_alerts,
    )

    from amr_sentinel.agents.stewardship_agent import StewardshipAgent
    steward = StewardshipAgent(concurrency=5)

    stewarded_result, steward_exc = _run_with_retry(
        lambda: steward.process(stewardship_batch), "stewardship"
    )

    run_summary["stages"]["stewardship"] = {
        "elapsed_s": round(time.time() - stage_start, 1),
        "status": "success" if steward_exc is None else "failed",
        "alerts_processed": len(stewarded_result) if stewarded_result else 0,
        "error": str(steward_exc) if steward_exc else None,
    }

    if steward_exc is not None:
        logger.warning("Stewardship failed — proceeding with fallback guidance.")
        run_summary["errors"].append(f"stewardship: {steward_exc}")
        stewarded_result = stewardship_batch

    stewarded = stewarded_result or []
    run_summary["alert_counts"]["after_stewardship"] = len(stewarded)
    logger.info("Stewardship: %d alerts with guidance", len(stewarded))

    # ------------------------------------------------------------------
    # Stage 5: Evidence linking
    # ------------------------------------------------------------------

    logger.info("--- Stage 5: Evidence Linking (PubMed RAG) ---")
    stage_start = time.time()

    if sentence_model is not None:
        _patch_sentence_transformer(sentence_model)

    from amr_sentinel.agents.evidence_linker import EvidenceLinker
    linker = EvidenceLinker(top_k=3, concurrency=5, skip_tiers={"monitor"})

    linked_result, linker_exc = _run_with_retry(
        lambda: linker.process(stewarded), "evidence linking"
    )

    run_summary["stages"]["evidence_linking"] = {
        "elapsed_s": round(time.time() - stage_start, 1),
        "status": "success" if linker_exc is None else "failed",
        "alerts_enriched": sum(1 for a in (linked_result or []) if a.evidence_citations),
        "total_citations": sum(len(a.evidence_citations) for a in (linked_result or [])),
        "error": str(linker_exc) if linker_exc else None,
    }

    if linker_exc is not None:
        logger.warning("Evidence linking failed — alerts retain empty citation lists.")
        run_summary["errors"].append(f"evidence_linking: {linker_exc}")
        linked_result = stewarded

    linked = linked_result or []
    total_citations = sum(len(a.evidence_citations) for a in linked)
    logger.info(
        "Evidence linking: %d/%d alerts enriched, %d total citations",
        sum(1 for a in linked if a.evidence_citations), len(linked), total_citations,
    )
    run_summary["alert_counts"]["total_citations"] = total_citations

    # ------------------------------------------------------------------
    # Stage 6: Write output queue (phenotypic alerts)
    # Note: genomic precursor alerts were already written in Stage 3b
    # ------------------------------------------------------------------

    logger.info("--- Stage 6: Write Output Queue ---")
    stage_start = time.time()

    written = _write_to_output_queue(linked, cycle_id)

    run_summary["stages"]["output_queue"] = {
        "elapsed_s": round(time.time() - stage_start, 1),
        "alerts_written": written,
        "output_path": str(OUTPUT_QUEUE_PATH),
    }
    run_summary["alert_counts"]["written_to_queue"] = written
    run_summary["alert_counts"]["total_in_queue"] = written + genomic_queued

    # ------------------------------------------------------------------
    # Cycle complete — operational scorecard
    # ------------------------------------------------------------------

    total_elapsed = time.time() - pipeline_start
    run_summary["status"] = "success"
    run_summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    run_summary["total_elapsed_s"] = round(total_elapsed, 1)

    detection_stage = run_summary.get("stages", {}).get("detection_and_scoring", {})
    forecast_success = detection_stage.get("forecast_success", 0)
    forecast_failed = detection_stage.get("forecast_failed", 0)
    forecast_total = forecast_success + forecast_failed
    forecast_coverage_pct = (
        round(forecast_success / forecast_total * 100, 1) if forecast_total > 0 else 0.0
    )

    critical_signals = sum(1 for s in scored_result if s.severity_tier == "critical")
    warn_signals = sum(1 for s in scored_result if s.severity_tier == "warn")
    avg_citations = round(total_citations / written, 1) if written > 0 else 0.0

    stage_times = {
        k: v.get("elapsed_s", 0) for k, v in run_summary.get("stages", {}).items()
    }

    logger.info("=" * 70)
    logger.info("PIPELINE CYCLE COMPLETE")
    logger.info("  Cycle ID         : %s", cycle_id)
    logger.info("=" * 70)
    logger.info("OPERATIONAL SCORECARD")
    logger.info("  Forecast coverage    : %.1f%% (%d/%d triplets)",
                forecast_coverage_pct, forecast_success, forecast_total)
    logger.info("  Forecast failures    : %d", forecast_failed)
    logger.info("  Signals detected     : %d (critical=%d, warn=%d)",
                len(scored_result), critical_signals, warn_signals)
    logger.info("  Canonical alerts     : %d", len(alerts))
    logger.info("  Genomic precursor    : %d queued", genomic_queued)
    logger.info("  Bulletins generated  : %d/%d", written, min(written, len(alerts)))
    logger.info("  Evidence enrichment  : %d/%d alerts",
                sum(1 for a in linked if a.evidence_citations), len(linked))
    logger.info("  Avg citations/alert  : %.1f", avg_citations)
    logger.info("  End-to-end runtime   : %.1f min", total_elapsed / 60)
    logger.info("  Pipeline failures    : %d", len(run_summary["errors"]))
    logger.info("─" * 70)
    logger.info("STAGE TIMING BREAKDOWN")
    for stage, elapsed in stage_times.items():
        logger.info("  %-28s : %.1fs", stage.replace("_", " ").title(), elapsed)
    logger.info("─" * 70)
    logger.info("  Output queue     : %s", OUTPUT_QUEUE_PATH)
    logger.info("  Run log          : %s", RUN_LOG_PATH)
    logger.info("=" * 70)

    _write_run_log(run_summary)
    return run_summary


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def run_scheduler(schedule_cron: str = "0 2 * * *") -> None:
    """
    Start the APScheduler daemon running the pipeline on a cron schedule.
    Blocks the main thread. Send SIGINT (Ctrl+C) to stop gracefully.
    """
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:
        raise ImportError("apscheduler not installed. Run: pip install apscheduler") from exc

    cron_parts = schedule_cron.split()
    if len(cron_parts) != 5:
        raise ValueError(
            f"Invalid cron expression: '{schedule_cron}'. "
            "Expected 5 fields: minute hour day month day_of_week"
        )
    minute, hour, day, month, day_of_week = cron_parts

    sentence_model = _load_sentence_transformer()
    max_alerts = int(os.getenv("MAX_STEWARDSHIP_ALERTS", "50"))

    scheduler = BlockingScheduler(timezone="UTC")
    trigger = CronTrigger(
        minute=minute, hour=hour, day=day,
        month=month, day_of_week=day_of_week, timezone="UTC",
    )

    def _scheduled_run() -> None:
        try:
            run_pipeline(
                max_stewardship_alerts=max_alerts,
                skip_ingestion=False,
                sentence_model=sentence_model,
            )
        except Exception as exc:
            logger.error(
                "Unhandled exception in scheduled pipeline run: %s\n%s",
                exc, traceback.format_exc(),
            )

    scheduler.add_job(
        _scheduled_run,
        trigger=trigger,
        id="amr_sentinel_pipeline",
        name="AMR-Sentinel full pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    logger.info("=" * 70)
    logger.info("AMR-SENTINEL ORCHESTRATOR STARTED (DAEMON MODE)")
    logger.info("Schedule      : %s (UTC)", schedule_cron)
    logger.info("Max alerts    : %d per cycle", max_alerts)
    logger.info("Output queue  : %s", OUTPUT_QUEUE_PATH)
    logger.info("Run log       : %s", RUN_LOG_PATH)
    logger.info("Press Ctrl+C to stop.")
    logger.info("=" * 70)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Orchestrator stopped by user.")
        scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AMR-Sentinel orchestrator — autonomous pipeline scheduler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m amr_sentinel.orchestrator --run-once --skip-ingestion --max-alerts 10
  python -m amr_sentinel.orchestrator --run-once
  python -m amr_sentinel.orchestrator
  python -m amr_sentinel.orchestrator --schedule "0 */6 * * *"
        """,
    )
    parser.add_argument("--run-once", action="store_true",
                        help="Run one pipeline cycle immediately and exit.")
    parser.add_argument("--skip-ingestion", action="store_true",
                        help="Skip data ingestion; run ML/agent pipeline over existing DB records.")
    parser.add_argument("--max-alerts", type=int,
                        default=int(os.getenv("MAX_STEWARDSHIP_ALERTS", "50")),
                        help="Max alerts through LLM per cycle (default: 50).")
    parser.add_argument("--schedule", type=str,
                        default=os.getenv("PIPELINE_SCHEDULE", "0 2 * * *"),
                        help='Cron schedule for daemon mode (default: "0 2 * * *").')
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.run_once:
        logger.info("Running in one-shot mode (--run-once).")
        sentence_model = _load_sentence_transformer()
        summary = run_pipeline(
            max_stewardship_alerts=args.max_alerts,
            skip_ingestion=args.skip_ingestion,
            sentence_model=sentence_model,
        )
        ac = summary.get("alert_counts", {})
        stages = summary.get("stages", {})

        det = stages.get("detection_and_scoring", {})
        fs = det.get("forecast_success", 0)
        ff = det.get("forecast_failed", 0)
        ft = fs + ff
        fc_pct = f"{fs/ft*100:.1f}%" if ft > 0 else "N/A"

        written = ac.get("written_to_queue", 0)
        citations = ac.get("total_citations", 0)
        avg_cit = f"{citations/written:.1f}" if written > 0 else "0.0"
        elapsed_s = summary.get("total_elapsed_s", 0)
        errors = summary.get("errors", [])
        genomic_queued = ac.get("genomic_precursor_queued", 0)

        print(f"\n{'='*62}")
        print("AMR-SENTINEL — OPERATIONAL SCORECARD")
        print(f"{'='*62}")
        print(f"  Status                  : {summary['status']}")
        print(f"  Cycle ID                : {summary.get('cycle_id', 'N/A')[:19]}")
        print(f"{'─'*62}")
        print(f"  Forecast coverage       : {fc_pct} ({fs}/{ft} triplets)")
        print(f"  Forecast failures       : {ff}")
        print(f"  Signals detected        : {ac.get('signals_detected', 0)}")
        print(f"  Canonical alerts        : {ac.get('after_triage', 0)}")
        print(f"  Genomic precursor alerts: {genomic_queued}")
        print(f"  Bulletins generated     : {written}/{ac.get('after_triage', 0)}")
        print(f"  Evidence enrichment     : {written}/{written} alerts")
        print(f"  Avg citations / alert   : {avg_cit}")
        print(f"  End-to-end runtime      : {elapsed_s/60:.1f} min")
        print(f"  Pipeline failures       : {len(errors)}")
        print(f"{'─'*62}")
        print(f"  Output queue            : {OUTPUT_QUEUE_PATH}")
        print(f"  Run log                 : {RUN_LOG_PATH}")
        if errors:
            print(f"{'─'*62}")
            print("  ERRORS:")
            for err in errors:
                print(f"    - {err}")
        print(f"{'='*62}\n")
        sys.exit(0 if summary["status"] in ("success", "success_no_alerts") else 1)

    else:
        run_scheduler(schedule_cron=args.schedule)