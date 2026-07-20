"""
AMR-Intel — State Transition Tracker (Task 5)
===============================================
Computes and stores the historical resistance tier per triplet per year,
enabling the platform to answer:

    "How long did Croatia take to go from WATCH to CRITICAL?"
    "Which threats are moving fastest right now?"
    "Which interventions caused IMPROVING transitions?"

The tracker reads all resistance_records, computes tier for each
(pathogen, antibiotic, country, year) observation, detects tier changes,
and writes to state_transitions with ON CONFLICT DO UPDATE.

Tier definitions:
    STABLE      resistance_rate < 5% AND no acceleration
    WATCH       resistance_rate < 10% AND accelerating (rate_change_1yr > 0.5pp)
    EMERGING    resistance_rate < 25% AND (new appearance OR fast acceleration)
    CRITICAL    resistance_rate >= 25%
    IMPROVING   resistance previously CRITICAL/EMERGING, now declining

Run:
    python -m amr_sentinel.models.state_transition_tracker
    python -m amr_sentinel.models.state_transition_tracker --dry-run
    python -m amr_sentinel.models.state_transition_tracker --pathogen "Klebsiella pneumoniae"

Inputs:
    - resistance_records table (WHO/ECDC/NGA)

Outputs:
    - state_transitions table (append-mode, upsert on conflict)

Dependencies:
    pip install sqlalchemy psycopg2-binary python-dotenv
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

load_dotenv()

logger = logging.getLogger("state_transition_tracker")

DATABASE_URL: str = (
    f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)

# ---------------------------------------------------------------------------
# Tier thresholds
# ---------------------------------------------------------------------------

STABLE_MAX   = 0.05   # < 5%
WATCH_MAX    = 0.10   # 5–10%
EMERGING_MAX = 0.25   # 10–25%
# CRITICAL     >= 25%

WATCH_ACCELERATION_MIN  = 0.005   # +0.5pp/year to trigger WATCH from STABLE
EMERGING_ACCELERATION   = 0.020   # +2pp/year to trigger EMERGING from WATCH
NEW_APPEARANCE_THRESHOLD = 0.02   # first observation above 2% = EMERGING

IMPROVING_DECLINE_MIN = -0.010    # -1pp/year while rate was previously CRITICAL


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

def classify_tier(
    rate: float,
    rate_change_1yr: Optional[float],
    previous_tier: Optional[str],
    is_first_observation: bool,
) -> str:
    """
    Classify resistance tier for one triplet-year observation.

    Args:
        rate: Current resistance rate (0-1)
        rate_change_1yr: Change vs prior year (None if no prior data)
        previous_tier: Tier in the previous year (None if first obs)
        is_first_observation: True if this is the first year we have data

    Returns:
        Tier string: STABLE / WATCH / EMERGING / CRITICAL / IMPROVING
    """
    # CRITICAL: absolute threshold — always CRITICAL regardless of trend
    if rate >= EMERGING_MAX:
        # Check for IMPROVING: was CRITICAL and now declining
        if (
            previous_tier in ("CRITICAL", "EMERGING")
            and rate_change_1yr is not None
            and rate_change_1yr <= IMPROVING_DECLINE_MIN
        ):
            return "IMPROVING"
        return "CRITICAL"

    # EMERGING: moderate resistance OR rapid acceleration from low base
    if rate >= WATCH_MAX:
        return "EMERGING"

    # New appearance above background: first observation at >2% = EMERGING
    if is_first_observation and rate >= NEW_APPEARANCE_THRESHOLD:
        return "EMERGING"

    # WATCH: low resistance but accelerating
    if rate >= STABLE_MAX:
        if rate_change_1yr is not None and rate_change_1yr >= WATCH_ACCELERATION_MIN:
            return "WATCH"
        return "WATCH"  # 5-10% is always at minimum WATCH

    # STABLE with acceleration check
    if rate_change_1yr is not None and rate_change_1yr >= WATCH_ACCELERATION_MIN:
        return "WATCH"  # accelerating even from low base

    return "STABLE"


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

ALL_RECORDS_QUERY = text("""
    SELECT
        pathogen_name,
        antibiotic_name,
        country_iso3,
        region_who,
        year,
        resistance_rate,
        sample_count,
        data_source
    FROM resistance_records
    WHERE resistance_rate IS NOT NULL
      AND resistance_rate < 1.0
    ORDER BY pathogen_name, antibiotic_name, country_iso3, year
""")

FILTERED_RECORDS_QUERY = text("""
    SELECT
        pathogen_name,
        antibiotic_name,
        country_iso3,
        region_who,
        year,
        resistance_rate,
        sample_count,
        data_source
    FROM resistance_records
    WHERE resistance_rate IS NOT NULL
      AND resistance_rate < 1.0
      AND pathogen_name ILIKE :pathogen_pattern
    ORDER BY pathogen_name, antibiotic_name, country_iso3, year
""")


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_transitions(
    session,
    pathogen_filter: Optional[str] = None,
) -> list[dict]:
    """
    Compute state transitions for all triplets in resistance_records.

    For each triplet, builds the full year-by-year tier history,
    detects tier changes, and returns a list of row dicts ready for
    upsert into state_transitions.

    Args:
        session: SQLAlchemy session
        pathogen_filter: Optional pathogen name substring filter

    Returns:
        List of dicts, one per (triplet, year) observation
    """
    if pathogen_filter:
        rows = session.execute(FILTERED_RECORDS_QUERY, {
            "pathogen_pattern": "%" + pathogen_filter + "%"
        }).fetchall()
    else:
        rows = session.execute(ALL_RECORDS_QUERY).fetchall()

    logger.info("Loaded %d resistance records for transition computation", len(rows))

    # Group by triplet
    triplets: dict[tuple, list] = defaultdict(list)
    for row in rows:
        key = (row.pathogen_name, row.antibiotic_name, row.country_iso3, row.region_who)
        triplets[key].append({
            "year": row.year,
            "rate": row.resistance_rate,
            "sample_count": row.sample_count,
            "data_source": row.data_source,
        })

    logger.info("Processing %d unique triplets", len(triplets))

    transition_rows = []
    tier_changes = 0

    for (pathogen, antibiotic, country, region), observations in triplets.items():
        # Sort by year
        obs = sorted(observations, key=lambda x: x["year"])

        prev_tier = None
        prev_rate = None
        years_in_tier = 0

        for i, o in enumerate(obs):
            year = o["year"]
            rate = o["rate"]
            is_first = (i == 0)

            # Year-over-year change
            rate_change_1yr = (rate - prev_rate) if prev_rate is not None else None

            # 3-year change
            rate_change_3yr = None
            if i >= 3:
                rate_change_3yr = rate - obs[i - 3]["rate"]

            # Acceleration (2nd derivative)
            acceleration = None
            if i >= 2:
                prev_change = obs[i-1]["rate"] - obs[i-2]["rate"]
                curr_change = rate - obs[i-1]["rate"]
                acceleration = curr_change - prev_change

            tier = classify_tier(rate, rate_change_1yr, prev_tier, is_first)
            changed = (tier != prev_tier) and (prev_tier is not None)

            if changed:
                tier_changes += 1
                years_in_previous = years_in_tier
                years_in_tier = 1
            else:
                years_in_tier += 1
                years_in_previous = None

            transition_rows.append({
                "pathogen_name": pathogen,
                "antibiotic_name": antibiotic,
                "country_iso3": country,
                "region_who": region,
                "year": year,
                "resistance_rate": rate,
                "sample_count": o["sample_count"],
                "data_source": o["data_source"],
                "tier": tier,
                "previous_tier": prev_tier,
                "tier_changed": changed,
                "years_in_previous_tier": years_in_previous,
                "rate_change_1yr": rate_change_1yr,
                "rate_change_3yr": rate_change_3yr,
                "acceleration": acceleration,
                "computed_at": datetime.now(timezone.utc),
            })

            prev_tier = tier
            prev_rate = rate

    logger.info(
        "Computed %d transition rows | %d tier changes detected",
        len(transition_rows), tier_changes,
    )
    return transition_rows


def write_transitions(
    session,
    transition_rows: list[dict],
) -> dict[str, int]:
    """
    Upsert transition rows into state_transitions table.

    ON CONFLICT (pathogen, antibiotic, country, year) DO UPDATE —
    so re-running the tracker refreshes all computed fields in place.

    Args:
        session: SQLAlchemy session
        transition_rows: Output of compute_transitions()

    Returns:
        Dict with inserted/updated counts
    """
    from amr_sentinel.db.models import StateTransition

    inserted = updated = 0
    BATCH = 500

    for i in range(0, len(transition_rows), BATCH):
        batch = transition_rows[i:i + BATCH]
        stmt = (
            pg_insert(StateTransition)
            .values(batch)
            .on_conflict_do_update(
                constraint="uq_state_transition_triplet_year",
                set_={
                    "tier": pg_insert(StateTransition).excluded.tier,
                    "previous_tier": pg_insert(StateTransition).excluded.previous_tier,
                    "tier_changed": pg_insert(StateTransition).excluded.tier_changed,
                    "years_in_previous_tier": pg_insert(StateTransition).excluded.years_in_previous_tier,
                    "rate_change_1yr": pg_insert(StateTransition).excluded.rate_change_1yr,
                    "rate_change_3yr": pg_insert(StateTransition).excluded.rate_change_3yr,
                    "acceleration": pg_insert(StateTransition).excluded.acceleration,
                    "computed_at": pg_insert(StateTransition).excluded.computed_at,
                },
            )
        )
        result = session.execute(stmt)
        inserted += result.rowcount

    session.commit()
    logger.info("Wrote %d transition rows to state_transitions", inserted)
    return {"written": inserted}


def run_tracker(
    pathogen_filter: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """
    Full pipeline: compute transitions and write to DB.

    Args:
        pathogen_filter: Optional substring filter on pathogen_name
        dry_run: If True, compute but do not write

    Returns:
        Summary dict
    """
    logger.info("=== State Transition Tracker starting ===")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    SessionFactory = sessionmaker(bind=engine)

    with SessionFactory() as session:
        rows = compute_transitions(session, pathogen_filter=pathogen_filter)

        if dry_run:
            logger.info("DRY RUN — %d rows computed, not written", len(rows))
            _print_summary(rows)
            return {"computed": len(rows), "written": 0, "dry_run": True}

        result = write_transitions(session, rows)

    _print_summary(rows)

    return {
        "computed": len(rows),
        "written": result["written"],
        "dry_run": False,
        "run_at": datetime.now(timezone.utc).isoformat(),
    }


def _print_summary(rows: list[dict]) -> None:
    """Print tier distribution summary."""
    from collections import Counter
    tier_counts = Counter(r["tier"] for r in rows)
    change_rows = [r for r in rows if r["tier_changed"]]

    print("\n" + "=" * 60)
    print("STATE TRANSITION TRACKER — SUMMARY")
    print("=" * 60)
    print(f"Total observations: {len(rows)}")
    print(f"Tier changes: {len(change_rows)}")
    print("\nTier distribution:")
    for tier in ["CRITICAL", "EMERGING", "WATCH", "STABLE", "IMPROVING"]:
        count = tier_counts.get(tier, 0)
        pct = round(count / len(rows) * 100, 1) if rows else 0
        bar = "█" * int(pct / 2)
        print(f"  {tier:12s} {count:5d}  {pct:5.1f}%  {bar}")

    print("\nNotable transitions (STABLE→CRITICAL fastest):")
    # Find triplets with fastest STABLE→CRITICAL progression
    triplet_histories: dict[tuple, list] = defaultdict(list)
    for r in rows:
        key = (r["pathogen_name"], r["antibiotic_name"], r["country_iso3"])
        triplet_histories[key].append(r)

    fast_transitions = []
    for key, history in triplet_histories.items():
        tiers = [h["tier"] for h in sorted(history, key=lambda x: x["year"])]
        if "STABLE" in tiers and "CRITICAL" in tiers:
            stable_idx = tiers.index("STABLE")
            try:
                critical_idx = next(
                    i for i, t in enumerate(tiers) if t == "CRITICAL" and i > stable_idx
                )
                years_taken = critical_idx - stable_idx
                rate = history[critical_idx]["resistance_rate"]
                fast_transitions.append((years_taken, key, rate))
            except StopIteration:
                pass

    fast_transitions.sort(key=lambda x: x[0])
    for years, (pathogen, antibiotic, country), rate in fast_transitions[:5]:
        print(
            f"  {pathogen[:25]:25s} / {antibiotic[:15]:15s} / {country}"
            f"  — {years} years  ({round(rate*100,1)}%)"
        )
    print("=" * 60)


# ---------------------------------------------------------------------------
# Query helpers used by the API
# ---------------------------------------------------------------------------

def get_triplet_history(
    session,
    pathogen_name: str,
    antibiotic_name: str,
    country_iso3: str,
) -> list[dict]:
    """
    Retrieve full state transition history for one triplet.
    Used by GET /alerts/{id}/history API endpoint.

    Args:
        session: SQLAlchemy session
        pathogen_name, antibiotic_name, country_iso3: triplet identity

    Returns:
        List of dicts ordered by year ascending
    """
    from amr_sentinel.db.models import StateTransition

    rows = (
        session.query(StateTransition)
        .filter(
            StateTransition.pathogen_name == pathogen_name,
            StateTransition.antibiotic_name == antibiotic_name,
            StateTransition.country_iso3 == country_iso3,
        )
        .order_by(StateTransition.year.asc())
        .all()
    )

    return [
        {
            "year": r.year,
            "tier": r.tier,
            "previous_tier": r.previous_tier,
            "tier_changed": r.tier_changed,
            "resistance_rate": r.resistance_rate,
            "rate_change_1yr": r.rate_change_1yr,
            "rate_change_3yr": r.rate_change_3yr,
            "acceleration": r.acceleration,
            "years_in_previous_tier": r.years_in_previous_tier,
            "data_source": r.data_source,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="State Transition Tracker — compute resistance tier history"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute but do not write to DB")
    parser.add_argument("--pathogen", type=str, default=None,
                        help="Filter to pathogen substring e.g. 'Klebsiella'")
    args = parser.parse_args()

    summary = run_tracker(
        pathogen_filter=args.pathogen,
        dry_run=args.dry_run,
    )

    print(f"\nDone. Computed: {summary['computed']} | Written: {summary['written']}")