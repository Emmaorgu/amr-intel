"""
amr_sentinel/models/emergence_scorer.py
=========================================
Computes Emergence Scores for all resistance triplets in the database.

The Emergence Score answers the question the severity scorer cannot:
    "Which country is BECOMING the next Bulgaria?"

The severity scorer measures how bad a situation is NOW.
The emergence scorer measures how fast a situation is GETTING WORSE.

A country with 10% resistance and accelerating is more interesting from
an early-warning perspective than a country stuck at 60% for a decade.

Score dimensions (0-100):
    1. Acceleration  (0-40) — rate of change, last 3yr vs prior 3yr
    2. New Appearance (0-30) — near-zero historically, now rising fast
    3. Spread Velocity (0-20) — regional co-acceleration signal
    4. Endemic Penalty (-20 to 0) — demote long-established burden

Classification (separate from emergence score):
    emerging        — score >= 60, low/mid current burden, accelerating
    endemic_critical — high current burden (>40%), long-established
    improving       — high current burden but falling trend
    watch           — score >= 30
    stable          — score < 30

Confidence:
    high    — >= 7 years of data, no missing baseline
    medium  — 4-6 years of data, or minor gaps
    low     — < 4 years, or missing baseline data

Acceleration Index (1-10 capped):
    Replaces raw percentage (e.g. +1065%) with a 1-10 scale.
    Operationally meaningful. Not a raw number.

Results are written to the emergence_scores table and exposed via
GET /emergence-radar API endpoint.

Usage:
    python -m amr_sentinel.models.emergence_scorer
    python -m amr_sentinel.models.emergence_scorer --top 20
    python -m amr_sentinel.models.emergence_scorer --dry-run

Dependencies:
    sqlalchemy, psycopg2-binary, python-dotenv
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from amr_sentinel.db.database import get_session
from amr_sentinel.db.models import EmergenceScore, ResistanceRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scoring thresholds
# ---------------------------------------------------------------------------

ACCELERATION_THRESHOLDS = [
    (1.00, 40),
    (0.50, 30),
    (0.25, 20),
    (0.10, 10),
]

NEW_APPEARANCE_THRESHOLDS = [
    (0.05, 0.20, 30),
    (0.05, 0.10, 20),
    (0.15, 0.30, 15),
]

# Strengthened endemic penalty — Bulgaria at 67.6% for 5+ years should
# not appear in the emerging tier at all.
ENDEMIC_PENALTY_THRESHOLDS = [
    (0.40, 7, -25),  # >40% for 7+ consecutive years → -25pts (removes from emerging)
    (0.40, 5, -20),  # >40% for 5+ consecutive years → -20pts
    (0.40, 3, -10),  # >40% for 3+ consecutive years → -10pts
]

SPREAD_VELOCITY_THRESHOLDS = [
    (3, 20),
    (2, 10),
]

# Classification thresholds
EMERGING_THRESHOLD = 60
WATCH_THRESHOLD = 30

# Endemic critical: high burden, long-established
ENDEMIC_CRITICAL_RATE = 0.40
ENDEMIC_CRITICAL_YEARS = 3

# Improving: high burden but falling
IMPROVING_RATE = 0.30
IMPROVING_LOOKBACK = 3


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class TripletTimeSeries(NamedTuple):
    pathogen_name: str
    antibiotic_name: str
    country_iso3: str
    region_who: str | None
    years: list[int]
    rates: list[float]


class EmergenceResult(NamedTuple):
    pathogen_name: str
    antibiotic_name: str
    country_iso3: str
    region_who: str | None
    emergence_score: int
    emergence_tier: str        # emerging | escalating | endemic_critical | improving | watch | stable
    acceleration_score: int
    new_appearance_score: int
    spread_velocity_score: int
    endemic_penalty: int
    acceleration_rate: float | None   # raw relative rate (stored in DB)
    acceleration_index: int | None    # 1-10 capped index (displayed)
    baseline_rate: float | None
    current_rate: float | None
    years_observed: int
    data_confidence: str              # high | medium | low
    has_baseline: bool
    drivers: list                     # human-readable "Why?" phrases for the dashboard


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def _compute_confidence(years: list[int], rates: list[float]) -> tuple[str, bool]:
    """
    Compute data confidence and baseline availability.

    Returns (confidence_level, has_baseline).
    confidence: high >= 7yr, medium 4-6yr, low < 4yr or missing baseline.
    has_baseline: True if earliest data point is available.
    """
    n = len([r for r in rates if r is not None])
    has_baseline = n >= 3 and rates[0] is not None

    if n >= 7 and has_baseline:
        confidence = "high"
    elif n >= 4:
        confidence = "medium"
    else:
        confidence = "low"

    if not has_baseline:
        confidence = "low"

    return confidence, has_baseline


# ---------------------------------------------------------------------------
# Acceleration index (1-10 cap)
# ---------------------------------------------------------------------------

def _acceleration_index(relative_rate: float | None) -> int | None:
    """
    Convert raw relative acceleration to a 1-10 index.

    Replaces operationally meaningless percentages like +1065%.
    Maps:
        >= 10x (1000%)  → 10
        >= 5x  (500%)   → 9
        >= 3x  (300%)   → 8
        >= 2x  (200%)   → 7
        >= 1.5x (150%)  → 6
        >= 1x  (100%)   → 5
        >= 0.5x (50%)   → 4
        >= 0.25x (25%)  → 3
        >= 0.10x (10%)  → 2
        > 0             → 1
        <= 0            → None (not accelerating)
    """
    if relative_rate is None or relative_rate <= 0:
        return None
    if relative_rate >= 10.0:
        return 10
    if relative_rate >= 5.0:
        return 9
    if relative_rate >= 3.0:
        return 8
    if relative_rate >= 2.0:
        return 7
    if relative_rate >= 1.5:
        return 6
    if relative_rate >= 1.0:
        return 5
    if relative_rate >= 0.5:
        return 4
    if relative_rate >= 0.25:
        return 3
    if relative_rate >= 0.10:
        return 2
    return 1


# ---------------------------------------------------------------------------
# Classification: emerging vs endemic_critical vs improving
# ---------------------------------------------------------------------------

def _classify_situation(
    years: list[int],
    rates: list[float],
    emergence_score: int,
) -> str:
    """
    Classify the epidemiological situation independently of the emergence score.

    This separates:
        emerging        — actively getting worse, not yet established
        endemic_critical — already bad, long-established
        improving       — high but falling
        watch           — moderate acceleration
        stable          — no signal

    Bulgaria at 67.6% for 5+ years = endemic_critical, not emerging.
    Malta accelerating from 0% to 30% = emerging.
    Lithuania falling from 66% to 61% = improving.
    """
    if not rates:
        return "stable"

    paired = sorted(zip(years, rates))
    sorted_rates = [r for _, r in paired if r is not None]

    if not sorted_rates:
        return "stable"

    current = sorted_rates[-1]
    recent = sorted_rates[max(0, len(sorted_rates) - IMPROVING_LOOKBACK):]

    # Check for endemic_critical: high burden for multiple years
    consecutive_high = 0
    for r in reversed(sorted_rates):
        if r >= ENDEMIC_CRITICAL_RATE:
            consecutive_high += 1
        else:
            break

    if consecutive_high >= ENDEMIC_CRITICAL_YEARS and current >= ENDEMIC_CRITICAL_RATE:
        # Check if it's actually improving (falling despite high burden)
        if len(recent) >= 2 and recent[-1] < recent[0]:
            return "improving"
        return "endemic_critical"

    # Check improving: high current but falling trend
    if current >= IMPROVING_RATE and len(recent) >= 2 and recent[-1] < recent[0]:
        return "improving"

    # Standard emergence tiers — distinguish emerging from escalating
    if emergence_score >= EMERGING_THRESHOLD:
        # baseline = average of earliest 3 data points
        early_rates = sorted_rates[:min(3, len(sorted_rates))]
        baseline_rate = sum(early_rates) / len(early_rates) if early_rates else None
        # True emergence: was near-zero, now rising
        if baseline_rate is not None and baseline_rate < 0.10:
            return "emerging"
        # Escalating: was already at moderate level, now accelerating
        return "escalating"
    elif emergence_score >= WATCH_THRESHOLD:
        return "watch"
    return "stable"


# ---------------------------------------------------------------------------
# Core scoring functions
# ---------------------------------------------------------------------------

def _score_acceleration(years: list[int], rates: list[float]) -> tuple[int, float | None]:
    """
    Score acceleration: last 3yr vs prior 3yr.
    Returns (score 0-40, relative_acceleration_rate).
    """
    if len(years) < 4:
        return 0, None

    paired = sorted(zip(years, rates), key=lambda x: x[0])
    sorted_rates = [float(r) for _, r in paired if r is not None]
    n = len(sorted_rates)
    if n < 4:
        return 0, None

    last_3 = sorted_rates[max(0, n - 3):]
    prior_3 = sorted_rates[max(0, n - 6):max(0, n - 3)]

    if not prior_3:
        return 0, None

    avg_last = sum(last_3) / len(last_3)
    avg_prior = sum(prior_3) / len(prior_3)

    if avg_prior < 0.01:
        abs_change = avg_last - avg_prior
        if abs_change > 0.15:
            return 40, None
        elif abs_change > 0.08:
            return 25, None
        elif abs_change > 0.03:
            return 10, None
        return 0, None

    relative_accel = (avg_last - avg_prior) / avg_prior

    for threshold, pts in ACCELERATION_THRESHOLDS:
        if relative_accel >= threshold:
            return pts, relative_accel

    return 0, relative_accel


def _score_new_appearance(years: list[int], rates: list[float]) -> int:
    """Score new resistance appearance. Returns 0-30."""
    if len(years) < 3:
        return 0

    paired = sorted(zip(years, rates), key=lambda x: x[0])
    sorted_rates = [float(r) for _, r in paired if r is not None]
    n = len(sorted_rates)

    baseline_rates = sorted_rates[:min(3, n - 1)]
    current_rates = sorted_rates[max(0, n - 2):]

    if not baseline_rates or not current_rates:
        return 0

    baseline = sum(baseline_rates) / len(baseline_rates)
    current = sum(current_rates) / len(current_rates)

    for base_threshold, current_threshold, pts in NEW_APPEARANCE_THRESHOLDS:
        if baseline < base_threshold and current > current_threshold:
            return pts

    return 0


def _score_endemic_penalty(years: list[int], rates: list[float]) -> int:
    """
    Penalise long-established high-burden situations.
    Strengthened thresholds ensure Bulgaria (67.6% for 5+ years) is
    excluded from the emerging tier entirely.
    Returns penalty -25 to 0.
    """
    if len(years) < 3:
        return 0

    paired = sorted(zip(years, rates), key=lambda x: x[0])
    sorted_rates = [float(r) for _, r in paired if r is not None]

    for threshold, min_years, penalty in ENDEMIC_PENALTY_THRESHOLDS:
        consecutive = 0
        for rate in reversed(sorted_rates):
            if rate >= threshold:
                consecutive += 1
            else:
                break
        if consecutive >= min_years:
            return penalty

    return 0


def _score_spread_velocity(
    triplet: TripletTimeSeries,
    all_triplets: dict[tuple[str, str, str], TripletTimeSeries],
) -> int:
    """Score regional co-acceleration. Returns 0-20."""
    if not triplet.region_who:
        return 0

    accelerating_countries = 0
    for (p, ab, c), other in all_triplets.items():
        if c == triplet.country_iso3:
            continue
        if p != triplet.pathogen_name or ab != triplet.antibiotic_name:
            continue
        if other.region_who != triplet.region_who:
            continue
        accel_score, _ = _score_acceleration(other.years, other.rates)
        if accel_score >= 10:
            accelerating_countries += 1

    for threshold, pts in SPREAD_VELOCITY_THRESHOLDS:
        if accelerating_countries >= threshold:
            return pts

    return 0


def _build_emergence_drivers(
    triplet: TripletTimeSeries,
    tier: str,
    accel_score: int,
    new_app_score: int,
    spread_score: int,
    endemic_penalty: int,
    accel_index: int | None,
    baseline_rate: float | None,
    current_rate: float | None,
    confidence: str,
    has_baseline: bool,
) -> list[str]:
    """
    Build human-readable driver phrases explaining the emergence score.
    These power the 'Why?' column in the dashboard and printed radar.
    """
    drivers = []

    if not has_baseline:
        drivers.append("⚠ No historical baseline — confidence limited")

    if new_app_score >= 30:
        bl = f"{baseline_rate * 100:.1f}%" if baseline_rate else "near-zero"
        cur = f"{current_rate * 100:.1f}%" if current_rate else "unknown"
        drivers.append(f"True new emergence: baseline {bl} → now {cur}")
    elif new_app_score >= 15:
        drivers.append("Rapid escalation from low baseline")

    if accel_index and accel_index >= 8:
        drivers.append(f"Extreme acceleration ({accel_index}/10 index)")
    elif accel_index and accel_index >= 5:
        drivers.append(f"Strong acceleration ({accel_index}/10 index)")
    elif accel_index and accel_index >= 3:
        drivers.append(f"Moderate acceleration ({accel_index}/10 index)")

    if spread_score >= 20:
        drivers.append("Regional spread event — 3+ countries co-accelerating")
    elif spread_score >= 10:
        drivers.append("Regional signal — 2 countries co-accelerating")

    if endemic_penalty <= -20:
        drivers.append("Long-established burden — not a new threat")
    elif endemic_penalty <= -10:
        drivers.append("Established endemic burden — partially penalised")

    if tier == "escalating":
        drivers.append("Escalating from moderate burden (not near-zero origin)")
    elif tier == "improving":
        drivers.append("High burden but trend is falling — situation improving")
    elif tier == "endemic_critical":
        drivers.append("Already dangerous — long-established critical burden")

    if confidence == "low":
        drivers.append("Low data confidence — interpret with caution")

    return drivers


def _compute_emergence(
    triplet: TripletTimeSeries,
    all_triplets: dict[tuple[str, str, str], TripletTimeSeries],
) -> EmergenceResult:
    """Compute the full emergence result for a single triplet."""
    accel_score, accel_rate = _score_acceleration(triplet.years, triplet.rates)
    new_app_score = _score_new_appearance(triplet.years, triplet.rates)
    spread_score = _score_spread_velocity(triplet, all_triplets)
    endemic_penalty = _score_endemic_penalty(triplet.years, triplet.rates)
    confidence, has_baseline = _compute_confidence(triplet.years, triplet.rates)

    raw_total = accel_score + new_app_score + spread_score + endemic_penalty
    total = max(0, min(100, raw_total))

    tier = _classify_situation(triplet.years, triplet.rates, total)

    paired = sorted(zip(triplet.years, triplet.rates))
    baseline_rate = paired[0][1] if paired else None
    current_rate = paired[-1][1] if paired else None

    accel_index = _acceleration_index(accel_rate)

    drivers = _build_emergence_drivers(
        triplet=triplet,
        tier=tier,
        accel_score=accel_score,
        new_app_score=new_app_score,
        spread_score=spread_score,
        endemic_penalty=endemic_penalty,
        accel_index=accel_index,
        baseline_rate=float(baseline_rate) if baseline_rate is not None else None,
        current_rate=float(current_rate) if current_rate is not None else None,
        confidence=confidence,
        has_baseline=has_baseline,
    )

    return EmergenceResult(
        pathogen_name=triplet.pathogen_name,
        antibiotic_name=triplet.antibiotic_name,
        country_iso3=triplet.country_iso3,
        region_who=triplet.region_who,
        emergence_score=total,
        emergence_tier=tier,
        acceleration_score=accel_score,
        new_appearance_score=new_app_score,
        spread_velocity_score=spread_score,
        endemic_penalty=endemic_penalty,
        acceleration_rate=round(accel_rate, 4) if accel_rate is not None else None,
        acceleration_index=accel_index,
        baseline_rate=round(float(baseline_rate), 4) if baseline_rate is not None else None,
        current_rate=round(float(current_rate), 4) if current_rate is not None else None,
        years_observed=len(triplet.years),
        data_confidence=confidence,
        has_baseline=has_baseline,
        drivers=drivers,
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def load_triplets_from_db() -> dict[tuple[str, str, str], TripletTimeSeries]:
    """Load all resistance time-series from the database, grouped by triplet."""
    logger.info("Loading resistance records from database...")

    with get_session() as session:
        records = session.query(
            ResistanceRecord.pathogen_name,
            ResistanceRecord.antibiotic_name,
            ResistanceRecord.country_iso3,
            ResistanceRecord.region_who,
            ResistanceRecord.year,
            ResistanceRecord.resistance_rate,
        ).order_by(
            ResistanceRecord.pathogen_name,
            ResistanceRecord.antibiotic_name,
            ResistanceRecord.country_iso3,
            ResistanceRecord.year,
        ).all()

    grouped: dict[tuple, dict] = defaultdict(
        lambda: {"region": None, "years": [], "rates": []}
    )

    for r in records:
        if r.resistance_rate is None:
            continue
        key = (r.pathogen_name, r.antibiotic_name, r.country_iso3)
        grouped[key]["region"] = r.region_who
        grouped[key]["years"].append(r.year)
        grouped[key]["rates"].append(float(r.resistance_rate))

    triplets = {}
    for (p, ab, c), data in grouped.items():
        if len(data["years"]) >= 3:
            triplets[(p, ab, c)] = TripletTimeSeries(
                pathogen_name=p,
                antibiotic_name=ab,
                country_iso3=c,
                region_who=data["region"],
                years=data["years"],
                rates=data["rates"],
            )

    logger.info("Loaded %d triplets with >= 3 years of data.", len(triplets))
    return triplets


def run_emergence_scoring(
    dry_run: bool = False,
    top_n: int | None = None,
) -> list[EmergenceResult]:
    """Compute emergence scores for all triplets and write to DB."""
    triplets = load_triplets_from_db()

    if not triplets:
        logger.error("No triplets found in database.")
        return []

    logger.info("Computing emergence scores for %d triplets...", len(triplets))

    results = []
    for triplet in triplets.values():
        result = _compute_emergence(triplet, triplets)
        results.append(result)

    results.sort(key=lambda r: r.emergence_score, reverse=True)

    if not dry_run:
        _write_results_to_db(results)

    return results[:top_n] if top_n else results


def _write_results_to_db(results: list[EmergenceResult]) -> None:
    """Write emergence scores — full refresh on each run."""
    logger.info("Writing %d emergence scores to database...", len(results))
    computed_at = datetime.now(timezone.utc)

    with get_session() as session:
        deleted = session.query(EmergenceScore).delete()
        logger.info("Cleared %d previous emergence scores.", deleted)

        for r in results:
            score = EmergenceScore(
                pathogen_name=r.pathogen_name,
                antibiotic_name=r.antibiotic_name,
                country_iso3=r.country_iso3,
                region_who=r.region_who,
                emergence_score=r.emergence_score,
                emergence_tier=r.emergence_tier,
                acceleration_score=r.acceleration_score,
                new_appearance_score=r.new_appearance_score,
                spread_velocity_score=r.spread_velocity_score,
                endemic_penalty=r.endemic_penalty,
                acceleration_rate=r.acceleration_rate,
                acceleration_index=r.acceleration_index,
                baseline_rate=r.baseline_rate,
                current_rate=r.current_rate,
                years_observed=r.years_observed,
                data_confidence=r.data_confidence,
                has_baseline=r.has_baseline,
                computed_at=computed_at,
            )
            session.add(score)

    logger.info("Emergence scores written successfully.")


def print_emergence_radar(results: list[EmergenceResult], top_n: int = 20) -> None:
    """Print formatted emergence radar — grouped by classification."""

    by_tier: dict[str, list] = defaultdict(list)
    for r in results:
        by_tier[r.emergence_tier].append(r)

    tier_order = ["emerging", "escalating", "endemic_critical", "improving", "watch", "stable"]
    tier_labels = {
        "emerging":        "EMERGING — True new emergence (was near-zero, now rising)",
        "escalating":      "ESCALATING — Accelerating from moderate burden",
        "endemic_critical":"ENDEMIC CRITICAL — Already dangerous, long-established",
        "improving":       "IMPROVING — High but decreasing",
        "watch":           "WATCH — Acceleration detected",
        "stable":          "STABLE — No signal",
    }

    print("\n" + "=" * 100)
    print("AMR-SENTINEL — RESISTANCE EMERGENCE RADAR")
    print("Situations classified by trajectory, not just current burden")
    print("=" * 100)

    shown = 0
    for tier in tier_order:
        tier_results = by_tier.get(tier, [])
        if not tier_results:
            continue
        if tier == "stable":
            continue  # stable = background noise, don't print by default

        print(f"\n── {tier_labels[tier]} ({len(tier_results)} triplets) ──")
        print(f"  {'Score':<7} {'Pathogen':<25} {'Ab':<14} {'Ctry':<6} "
              f"{'Current':<9} {'Accel Idx':<10} {'Confidence':<10} {'Data'}")
        print(f"  {'─'*85}")

        for r in tier_results[:top_n]:
            current_pct = f"{r.current_rate * 100:.1f}%" if r.current_rate else "—"
            accel_idx = f"{r.acceleration_index}/10" if r.acceleration_index else "—"
            conf = r.data_confidence.upper()
            data_note = "" if r.has_baseline else "⚠ no baseline"

            print(
                f"  {r.emergence_score:<7} {r.pathogen_name[:24]:<25} "
                f"{r.antibiotic_name[:13]:<14} {r.country_iso3:<6} "
                f"{current_pct:<9} {accel_idx:<10} {conf:<10} {data_note}"
            )
            # Print driver phrases (Why?)
            for driver in r.drivers[:3]:  # show top 3 drivers
                print(f"    → {driver}")

            shown += 1
            if shown >= top_n:
                break
        if shown >= top_n:
            break

    print("\n" + "=" * 100)
    for tier in tier_order:
        count = len(by_tier.get(tier, []))
        if count:
            print(f"  {tier_labels[tier]}: {count}")
    print("=" * 100)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Compute Resistance Emergence Scores."
    )
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--tier",
        choices=["emerging", "escalating", "endemic_critical", "improving", "watch", "stable"],
    )
    args = parser.parse_args()

    results = run_emergence_scoring(dry_run=args.dry_run)

    if not results:
        print("No results.")
        sys.exit(1)

    if args.tier:
        results = [r for r in results if r.emergence_tier == args.tier]

    print_emergence_radar(results, top_n=args.top)