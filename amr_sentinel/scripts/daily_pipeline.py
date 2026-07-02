"""
amr_sentinel/scripts/daily_pipeline.py

Render Cron Job entry point — runs the full AMR-Intel pipeline once and exits.

This script is designed to be called by a Render Cron Job on a daily schedule
(e.g. 02:00 UTC). It runs the complete pipeline:
  1. Data ingestion (GLASS + ECDC + NDARO delta pull)
  2. Feature engineering
  3. Anomaly detection + severity scoring
  4. Triage agent
  5. Stewardship agent (bulletin generation)
  6. Evidence linker (PubMed citations)
  7. Alert writer (PostgreSQL)
  8. Bulletin regeneration for any alerts missing structured format

Render Cron Job config (render.yaml):
  - type: cron
    name: amr-intel-daily-pipeline
    env: python
    schedule: "0 2 * * *"   # 02:00 UTC daily
    buildCommand: pip install -r requirements-render.txt
    startCommand: python -m amr_sentinel.scripts.daily_pipeline

Environment variables required (set in Render dashboard):
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
  ANTHROPIC_API_KEY
  NCBI_API_KEY (optional, increases NDARO rate limits)

Outputs:
  - New alerts written to PostgreSQL
  - Structured bulletins generated for all new alerts
  - Exit code 0 on success, 1 on failure

Dependencies:
  All from requirements-render.txt
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# ── Path bootstrap ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("daily_pipeline")


def run_pipeline() -> bool:
    """
    Execute the full AMR-Intel pipeline in sequence.

    Returns
    -------
    bool
        True if pipeline completed without critical errors, False otherwise.
    """
    start_time = time.time()
    logger.info("=" * 60)
    logger.info("AMR-Intel Daily Pipeline — START")
    logger.info("=" * 60)

    errors: list[str] = []

    # ── Stage 1: Data ingestion ───────────────────────────────────────────────
    logger.info("Stage 1/7 — Data ingestion")
    try:
        from amr_sentinel.ingestion.glass_ingestor import run as run_glass
        from amr_sentinel.ingestion.ecdc_ingestor import run as run_ecdc
        run_glass()
        logger.info("  ✓ GLASS ingestor complete")
        run_ecdc()
        logger.info("  ✓ ECDC ingestor complete")
    except Exception as exc:
        logger.error("  ✗ Ingestion error: %s", exc)
        errors.append("ingestion: " + str(exc))

    # NDARO delta pull — non-fatal if it fails (large download)
    try:
        from amr_sentinel.ingestion.ndaro_ingestor import run as run_ndaro
        run_ndaro()
        logger.info("  ✓ NDARO ingestor complete")
    except Exception as exc:
        logger.warning("  ⚠ NDARO ingestor skipped: %s", exc)

    # ── Stage 2: Feature engineering ─────────────────────────────────────────
    logger.info("Stage 2/7 — Feature engineering")
    try:
        from amr_sentinel.models.feature_engineering import build_features
        build_features()
        logger.info("  ✓ Feature engineering complete")
    except Exception as exc:
        logger.error("  ✗ Feature engineering error: %s", exc)
        errors.append("feature_engineering: " + str(exc))

    # ── Stage 3: Anomaly detection + severity scoring ─────────────────────────
    logger.info("Stage 3/7 — Anomaly detection")
    try:
        from amr_sentinel.models.anomaly_detector import run as run_anomaly
        from amr_sentinel.models.severity_scorer import run as run_severity
        signals = run_anomaly()
        scored = run_severity(signals)
        logger.info("  ✓ %d signals scored", len(scored))
    except Exception as exc:
        logger.error("  ✗ Anomaly detection error: %s", exc)
        errors.append("anomaly_detection: " + str(exc))
        scored = []

    # ── Stage 4: Triage agent ─────────────────────────────────────────────────
    logger.info("Stage 4/7 — Triage agent")
    try:
        from amr_sentinel.agents.triage_agent import TriageAgent
        triage = TriageAgent()
        alerts = triage.process(scored)
        logger.info("  ✓ %d alerts triaged", len(alerts))
    except Exception as exc:
        logger.error("  ✗ Triage error: %s", exc)
        errors.append("triage: " + str(exc))
        alerts = []

    # ── Stage 5: Stewardship agent ────────────────────────────────────────────
    logger.info("Stage 5/7 — Stewardship agent")
    try:
        from amr_sentinel.agents.stewardship_agent import StewardshipAgent
        steward = StewardshipAgent()
        steward.process(alerts)
        logger.info("  ✓ Stewardship bulletins generated")
    except Exception as exc:
        logger.error("  ✗ Stewardship error: %s", exc)
        errors.append("stewardship: " + str(exc))

    # ── Stage 6: Evidence linker ──────────────────────────────────────────────
    logger.info("Stage 6/7 — Evidence linker")
    try:
        from amr_sentinel.agents.evidence_linker import EvidenceLinker
        linker = EvidenceLinker()
        linker.enrich(alerts)
        logger.info("  ✓ Evidence enrichment complete")
    except Exception as exc:
        logger.error("  ✗ Evidence linker error: %s", exc)
        errors.append("evidence_linker: " + str(exc))

    # ── Stage 7: Alert writer ─────────────────────────────────────────────────
    logger.info("Stage 7/7 — Alert writer")
    try:
        from amr_sentinel.db.alert_writer import write_alerts
        written = write_alerts(alerts)
        logger.info("  ✓ %d alerts written to DB", written)
    except Exception as exc:
        logger.error("  ✗ Alert writer error: %s", exc)
        errors.append("alert_writer: " + str(exc))

    # ── Post-run: backfill any missing structured bulletins ───────────────────
    logger.info("Post-run — Bulletin backfill check")
    try:
        from amr_sentinel.scripts.regenerate_bulletins import regenerate_alerts
        result = regenerate_alerts(skip_structured=True, delay_seconds=1.5)
        if result["updated"] > 0:
            logger.info("  ✓ %d bulletins regenerated", result["updated"])
        else:
            logger.info("  ✓ All bulletins already structured")
    except Exception as exc:
        logger.warning("  ⚠ Bulletin backfill skipped: %s", exc)

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.time() - start_time
    logger.info("=" * 60)
    if errors:
        logger.error(
            "Pipeline complete in %.1fs with %d error(s): %s",
            elapsed, len(errors), "; ".join(errors),
        )
        logger.info("=" * 60)
        return False
    else:
        logger.info("Pipeline complete in %.1fs — 0 errors", elapsed)
        logger.info("=" * 60)
        return True


if __name__ == "__main__":
    # Validate required env vars before starting
    required = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "ANTHROPIC_API_KEY"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    success = run_pipeline()
    sys.exit(0 if success else 1)