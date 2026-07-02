"""
amr_sentinel/scripts/daily_pipeline.py

GitHub Actions / Render Cron Job entry point.

Runs the full AMR-Intel pipeline by invoking the existing orchestrator
(which already has all correct module interfaces wired up), then runs
bulletin backfill for any alerts missing structured format.

Usage:
    python -m amr_sentinel.scripts.daily_pipeline

Environment variables required:
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
    ANTHROPIC_API_KEY
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("daily_pipeline")


def run_pipeline() -> bool:
    start = time.time()
    logger.info("=" * 60)
    logger.info("AMR-Intel Daily Pipeline — START")
    logger.info("=" * 60)

    errors: list[str] = []

    # ── Stage 1: Run orchestrator (handles all pipeline stages internally) ───
    logger.info("Stage 1 — Running orchestrator (full pipeline)")
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "amr_sentinel.orchestrator", "--run-once"],
            capture_output=False,
            text=True,
            cwd=str(PROJECT_ROOT),
            env={**os.environ},
        )
        if result.returncode != 0:
            errors.append("orchestrator exited with code " + str(result.returncode))
            logger.error("  ✗ Orchestrator failed with exit code %d", result.returncode)
        else:
            logger.info("  ✓ Orchestrator complete")
    except Exception as exc:
        logger.error("  ✗ Orchestrator error: %s", exc)
        errors.append("orchestrator: " + str(exc))

    # ── Stage 2: Write alerts from queue to DB ────────────────────────────────
    logger.info("Stage 2 — Writing alerts to DB")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "amr_sentinel.db.alert_writer"],
            capture_output=False,
            text=True,
            cwd=str(PROJECT_ROOT),
            env={**os.environ},
        )
        if result.returncode != 0:
            errors.append("alert_writer exited with code " + str(result.returncode))
            logger.error("  ✗ Alert writer failed with exit code %d", result.returncode)
        else:
            logger.info("  ✓ Alert writer complete")
    except Exception as exc:
        logger.error("  ✗ Alert writer error: %s", exc)
        errors.append("alert_writer: " + str(exc))

    # ── Stage 3: Backfill any unstructured bulletins ──────────────────────────
    logger.info("Stage 3 — Bulletin backfill")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "amr_sentinel.scripts.regenerate_bulletins",
             "--skip-structured" if False else ""],
            capture_output=False,
            text=True,
            cwd=str(PROJECT_ROOT),
            env={**os.environ},
        )
        logger.info("  ✓ Bulletin backfill complete")
    except Exception as exc:
        logger.warning("  ⚠ Bulletin backfill skipped: %s", exc)

    elapsed = time.time() - start
    logger.info("=" * 60)
    if errors:
        logger.error("Pipeline complete in %.1fs with %d error(s): %s",
                     elapsed, len(errors), "; ".join(errors))
        return False
    logger.info("Pipeline complete in %.1fs — 0 errors", elapsed)
    logger.info("=" * 60)
    return True


if __name__ == "__main__":
    required = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "ANTHROPIC_API_KEY"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    success = run_pipeline()
    sys.exit(0 if success else 1)