"""
amr_sentinel/scripts/regenerate_bulletins.py

Backfills structured stewardship bulletins for all phenotypic alerts in the database.

Reads every phenotypic alert (signal_type != 'genomic_precursor'), calls the updated
stewardship agent on each, and writes the resulting structured bulletin back to the
`stewardship_guidance` column in-place. Alert IDs, scores, citations, and all other
columns are left completely untouched.

This script exists because all alerts generated before the June 28 2026 stewardship
agent update have unstructured prose bulletins. The dashboard's renderStructuredBulletin()
function correctly renders the new 6-section format but falls back to paragraph rendering
for old bulletins. Running this script once brings all historic alerts into the new format.

Inputs:
  - PostgreSQL DB via DB_USER / DB_PASSWORD / DB_HOST / DB_PORT / DB_NAME env vars
  - Anthropic API key via ANTHROPIC_API_KEY env var
  - Optional --alert-id to regenerate a single alert (for testing)
  - Optional --dry-run to preview without writing to DB

Outputs:
  - Updated `stewardship_guidance` TEXT in `alerts` table for each processed alert
  - Console progress log with success/failure counts

Dependencies:
  - anthropic (already in requirements.txt)
  - sqlalchemy (already in requirements.txt)
  - python-dotenv (already in requirements.txt)

Usage:
  python -m amr_sentinel.scripts.regenerate_bulletins
  python -m amr_sentinel.scripts.regenerate_bulletins --dry-run
  python -m amr_sentinel.scripts.regenerate_bulletins --alert-id <uuid>
  python -m amr_sentinel.scripts.regenerate_bulletins --batch-size 5 --delay 2.0
"""

import argparse
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Path bootstrap — works whether invoked as a module or a script
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from amr_sentinel.db.database import SessionLocal
from amr_sentinel.db.models import Alert
from amr_sentinel.agents.stewardship_agent import StewardshipAgent

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("regenerate_bulletins")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_alert_dict(alert: Alert) -> dict:
    """
    Convert an SQLAlchemy Alert ORM object into the dict format that
    StewardshipAgent.generate_bulletin() expects.

    Parameters
    ----------
    alert : Alert
        ORM row from the alerts table.

    Returns
    -------
    dict
        Keys matching the stewardship agent's expected input schema.
    """
    extra: dict = alert.extra_data or {}

    return {
        "alert_id": str(alert.id),
        "pathogen_name": alert.pathogen_name,
        "antibiotic_name": alert.antibiotic_name,
        "country_iso3": alert.country_iso3,
        "severity_score": alert.severity_score,
        "severity_tier": alert.severity_tier,
        "signal_type": alert.signal_type,
        "current_resistance": alert.current_resistance,
        "forecasted_rate": alert.forecasted_rate,
        "deviation_magnitude": alert.deviation_magnitude,
        "trend_direction": alert.trend_direction,
        # genomic-specific fields (present on genomic_precursor alerts only)
        "gene_name": extra.get("gene_name"),
        "isolate_count": extra.get("isolate_count"),
        "doubling_time_years": extra.get("doubling_time_years"),
        "days_to_threshold": extra.get("days_to_threshold"),
        "phenotypic_resistance": extra.get("phenotypic_resistance"),
        # evidence citations already stored — pass through for context
        "evidence_citations": alert.evidence_citations or [],
        # emergence score driver phrases if present
        "why_phrase": extra.get("why_phrase", ""),
    }


def _is_structured(bulletin: Optional[str]) -> bool:
    """
    Return True if the bulletin already contains the structured section markers
    introduced in the June 28 2026 stewardship agent update.

    Parameters
    ----------
    bulletin : str or None
        Raw bulletin text from the DB.

    Returns
    -------
    bool
        True if bulletin is already in the new structured format.
    """
    if not bulletin:
        return False
    required_markers = ["EXECUTIVE SUMMARY", "SITUATION ASSESSMENT", "RECOMMENDED ACTIONS"]
    return all(marker in bulletin for marker in required_markers)


# ---------------------------------------------------------------------------
# WHO region lookup (derived from country_iso3 where who_region is missing)
# ---------------------------------------------------------------------------
_COUNTRY_TO_WHO_REGION: dict = {
    # AFRO
    "NGA": "AFRO", "GHA": "AFRO", "KEN": "AFRO", "ZAF": "AFRO", "ETH": "AFRO",
    "CMR": "AFRO", "UGA": "AFRO", "TZA": "AFRO", "SEN": "AFRO", "MLI": "AFRO",
    "COD": "AFRO", "MDG": "AFRO", "MOZ": "AFRO", "ZMB": "AFRO", "ZWE": "AFRO",
    # EURO
    "BGR": "EURO", "ROU": "EURO", "CYP": "EURO", "HRV": "EURO", "GRC": "EURO",
    "ITA": "EURO", "DEU": "EURO", "FRA": "EURO", "GBR": "EURO", "POL": "EURO",
    "SVK": "EURO", "CZE": "EURO", "HUN": "EURO", "LTU": "EURO", "LVA": "EURO",
    "EST": "EURO", "FIN": "EURO", "SWE": "EURO", "NOR": "EURO", "DNK": "EURO",
    "NLD": "EURO", "BEL": "EURO", "AUT": "EURO", "CHE": "EURO", "ESP": "EURO",
    "PRT": "EURO", "IRL": "EURO", "MLT": "EURO", "LUX": "EURO", "SVN": "EURO",
    "HRV": "EURO", "BIH": "EURO", "MKD": "EURO", "SRB": "EURO", "MNE": "EURO",
    "ALB": "EURO", "XKX": "EURO", "RUS": "EURO", "UKR": "EURO", "BLR": "EURO",
    "MDA": "EURO", "GEO": "EURO", "ARM": "EURO", "AZE": "EURO", "ISL": "EURO",
    # AMRO/PAHO
    "USA": "AMRO", "CAN": "AMRO", "MEX": "AMRO", "BRA": "AMRO", "ARG": "AMRO",
    "COL": "AMRO", "PER": "AMRO", "CHL": "AMRO", "VEN": "AMRO", "ECU": "AMRO",
    # SEARO
    "IND": "SEARO", "BGD": "SEARO", "THA": "SEARO", "IDN": "SEARO", "MMR": "SEARO",
    "NPL": "SEARO", "LKA": "SEARO", "MDV": "SEARO", "BTN": "SEARO", "PRK": "SEARO",
    # WPRO
    "CHN": "WPRO", "JPN": "WPRO", "KOR": "WPRO", "AUS": "WPRO", "NZL": "WPRO",
    "PHL": "WPRO", "VNM": "WPRO", "MYS": "WPRO", "SGP": "WPRO", "KHM": "WPRO",
    # EMRO
    "PAK": "EMRO", "IRN": "EMRO", "IRQ": "EMRO", "SAU": "EMRO", "EGY": "EMRO",
    "AFG": "EMRO", "YEM": "EMRO", "LBN": "EMRO", "SYR": "EMRO", "JOR": "EMRO",
    "TUN": "EMRO", "MAR": "EMRO", "DZA": "EMRO", "LBY": "EMRO", "SDN": "EMRO",
}

_ANTIBIOTIC_CLASS_MAP: dict = {
    # Carbapenems
    "imipenem": "Carbapenem", "meropenem": "Carbapenem", "ertapenem": "Carbapenem",
    "doripenem": "Carbapenem",
    # Glycopeptides
    "vancomycin": "Glycopeptide", "teicoplanin": "Glycopeptide",
    # Cephalosporins
    "ceftriaxone": "3rd-gen Cephalosporin", "cefotaxime": "3rd-gen Cephalosporin",
    "ceftazidime": "3rd-gen Cephalosporin", "cefepime": "4th-gen Cephalosporin",
    # Penicillins/Beta-lactams
    "ampicillin": "Aminopenicillin", "amoxicillin": "Aminopenicillin",
    "piperacillin": "Ureidopenicillin", "oxacillin": "Penicillinase-resistant penicillin",
    "methicillin": "Penicillinase-resistant penicillin",
    # Fluoroquinolones
    "ciprofloxacin": "Fluoroquinolone", "levofloxacin": "Fluoroquinolone",
    "moxifloxacin": "Fluoroquinolone",
    # Aminoglycosides
    "gentamicin": "Aminoglycoside", "amikacin": "Aminoglycoside",
    "tobramycin": "Aminoglycoside",
    # Polymyxins
    "colistin": "Polymyxin", "polymyxin b": "Polymyxin",
    # Oxazolidinones
    "linezolid": "Oxazolidinone",
    # Macrolides
    "erythromycin": "Macrolide", "azithromycin": "Macrolide",
    # Tetracyclines
    "tetracycline": "Tetracycline", "doxycycline": "Tetracycline",
}


def _derive_missing_attributes(alert) -> None:
    """
    Patch missing ORM attributes that the stewardship agent's _build_user_prompt()
    accesses but that may not be columns on the alerts table.

    Derives:
      - alert.who_region  — from country_iso3 lookup
      - alert.antibiotic_class — from antibiotic_name lookup
      - alert.evidence_years  — from data_year column (list of one year)

    Modifies the alert object in-place. Safe to call multiple times.
    """
    # who_region
    if not getattr(alert, "who_region", None):
        alert.who_region = _COUNTRY_TO_WHO_REGION.get(  # type: ignore[attr-defined]
            alert.country_iso3, "GLOBAL"
        )

    # antibiotic_class
    if not getattr(alert, "antibiotic_class", None):
        key = (alert.antibiotic_name or "").lower().strip()
        alert.antibiotic_class = _ANTIBIOTIC_CLASS_MAP.get(  # type: ignore[attr-defined]
            key, "Unknown"
        )

    # evidence_years — agent does: ", ".join(str(y) for y in alert.evidence_years)
    if not getattr(alert, "evidence_years", None):
        year = getattr(alert, "data_year", None)
        alert.evidence_years = [year] if year else ["N/A"]  # type: ignore[attr-defined]


def regenerate_alerts(
    *,
    dry_run: bool = False,
    alert_id: Optional[str] = None,
    skip_structured: bool = True,
    batch_size: int = 10,
    delay_seconds: float = 1.0,
) -> dict:
    """
    Main regeneration loop.

    Queries the DB for phenotypic alerts (signal_type != 'genomic_precursor'),
    calls StewardshipAgent.generate_bulletin() on each, and writes the result
    back to `stewardship_guidance`.

    Parameters
    ----------
    dry_run : bool
        If True, generate bulletins but do not write to the DB.
    alert_id : str, optional
        If provided, regenerate only this single alert UUID.
    skip_structured : bool
        If True (default), skip alerts that already have structured bulletins.
        Set to False to force-regenerate everything.
    batch_size : int
        Number of alerts to process before logging a progress checkpoint.
    delay_seconds : float
        Seconds to sleep between Anthropic API calls to avoid rate-limiting.

    Returns
    -------
    dict
        Summary counters: total, updated, skipped, failed.
    """
    agent = StewardshipAgent()

    counters = {
        "total": 0,
        "updated": 0,
        "skipped_already_structured": 0,
        "skipped_no_data": 0,
        "failed": 0,
    }

    db: Session = SessionLocal()
    try:
        # Build query — phenotypic alerts only
        query = db.query(Alert).filter(
            Alert.signal_type != "genomic_precursor"
        )

        if alert_id:
            try:
                parsed_id = uuid.UUID(alert_id)
            except ValueError:
                logger.error("Invalid alert_id format: %s", alert_id)
                return counters
            query = query.filter(Alert.id == parsed_id)

        alerts = query.order_by(Alert.created_at.desc()).all()
        counters["total"] = len(alerts)
        logger.info("Found %d phenotypic alerts to process.", len(alerts))

        if not alerts:
            logger.info("Nothing to do.")
            return counters

        for idx, alert in enumerate(alerts, start=1):

            # ------------------------------------------------------------------
            # Skip check
            # ------------------------------------------------------------------
            if skip_structured and _is_structured(alert.stewardship_guidance):
                logger.debug(
                    "[%d/%d] %s — already structured, skipping.",
                    idx, len(alerts), alert.id,
                )
                counters["skipped_already_structured"] += 1
                continue

            if not alert.pathogen_name or not alert.antibiotic_name or not alert.country_iso3:
                logger.warning(
                    "[%d/%d] %s — missing core fields, skipping.",
                    idx, len(alerts), alert.id,
                )
                counters["skipped_no_data"] += 1
                continue

            # ------------------------------------------------------------------
            # Generate
            # ------------------------------------------------------------------
            logger.info(
                "[%d/%d] Generating bulletin for %s / %s / %s  (score=%s tier=%s)...",
                idx, len(alerts),
                alert.pathogen_name,
                alert.antibiotic_name,
                alert.country_iso3,
                alert.severity_score,
                alert.severity_tier,
            )

            try:
                # Alias PK: agent uses alert.alert_id internally; our PK column is `id`
                if not getattr(alert, "alert_id", None):
                    alert.alert_id = alert.id  # type: ignore[attr-defined]

                # Derive attributes the agent prompt needs but that aren't DB columns
                _derive_missing_attributes(alert)

                # process() mutates alert.stewardship_guidance in-place and returns the list
                agent.process([alert])
                new_bulletin: str = alert.stewardship_guidance or ""
                if not new_bulletin:
                    raise ValueError("process() returned empty bulletin for alert " + str(alert.id))
            except Exception as exc:
                logger.error(
                    "[%d/%d] %s — bulletin generation failed: %s",
                    idx, len(alerts), alert.id, exc,
                )
                counters["failed"] += 1
                time.sleep(delay_seconds)
                continue

            # ------------------------------------------------------------------
            # Write
            # ------------------------------------------------------------------
            if dry_run:
                logger.info(
                    "[%d/%d] DRY RUN — would update %s. Preview (first 200 chars):\n%s",
                    idx, len(alerts), alert.id, new_bulletin[:200],
                )
            else:
                try:
                    alert.stewardship_guidance = new_bulletin
                    db.commit()
                    logger.info(
                        "[%d/%d] ✓ Updated %s (%s/%s/%s).",
                        idx, len(alerts),
                        alert.id,
                        alert.pathogen_name,
                        alert.antibiotic_name,
                        alert.country_iso3,
                    )
                except Exception as exc:
                    db.rollback()
                    logger.error(
                        "[%d/%d] %s — DB write failed: %s",
                        idx, len(alerts), alert.id, exc,
                    )
                    counters["failed"] += 1
                    continue

            counters["updated"] += 1

            # Progress checkpoint
            if idx % batch_size == 0:
                logger.info(
                    "Progress: %d/%d processed — updated=%d skipped=%d failed=%d",
                    idx, len(alerts),
                    counters["updated"],
                    counters["skipped_already_structured"],
                    counters["failed"],
                )

            # Rate-limit courtesy pause
            time.sleep(delay_seconds)

    finally:
        db.close()

    return counters


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill structured stewardship bulletins for all phenotypic alerts "
            "in the AMR-Sentinel database."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate bulletins but do not write to DB.",
    )
    parser.add_argument(
        "--alert-id",
        type=str,
        default=None,
        metavar="UUID",
        help="Regenerate a single alert by UUID (for testing).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even alerts that already have structured bulletins.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        metavar="N",
        help="Log a progress checkpoint every N alerts (default: 10).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="Seconds to sleep between Anthropic API calls (default: 1.0).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set. Cannot call stewardship agent.")
        sys.exit(1)

    logger.info("=== AMR-Sentinel bulletin regeneration ===")
    if args.dry_run:
        logger.info("DRY RUN mode — no DB writes.")
    if args.force:
        logger.info("FORCE mode — regenerating all alerts including already-structured ones.")

    results = regenerate_alerts(
        dry_run=args.dry_run,
        alert_id=args.alert_id,
        skip_structured=not args.force,
        batch_size=args.batch_size,
        delay_seconds=args.delay,
    )

    logger.info(
        "\n=== COMPLETE ===\n"
        "  Total alerts found:          %d\n"
        "  Bulletins updated:           %d\n"
        "  Already structured (skipped):%d\n"
        "  Missing data (skipped):      %d\n"
        "  Failed:                      %d",
        results["total"],
        results["updated"],
        results["skipped_already_structured"],
        results["skipped_no_data"],
        results["failed"],
    )

    if results["failed"] > 0:
        logger.warning(
            "%d bulletin(s) failed to regenerate. Re-run with --alert-id <uuid> "
            "to retry individual failures.",
            results["failed"],
        )
        sys.exit(1)