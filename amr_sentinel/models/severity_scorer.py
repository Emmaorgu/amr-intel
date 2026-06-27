"""
amr_sentinel/models/severity_scorer.py
========================================
Severity scorer for AMR-Sentinel — Sprint 5 Task 5.3 update.

What changed in Task 5.3:
    Added Dimension 5: Impact-weighted threat score (0-10 points)
    Combines:
        - WHO pathogen mortality weight (deaths per 100k attributable to this pathogen)
        - Treatment scarcity (alternatives remaining when this antibiotic fails)
        - Hospital burden multiplier (ICU-associated pathogens score higher)

    This ensures the score reflects clinical consequences, not just
    resistance behaviour. Two signals with equal resistance rates and
    deviation magnitudes can have very different impact scores based on
    whether the antibiotic that failed is the last available option.

Scoring methodology (5 dimensions):
    1. Pathogen priority weight    (0-30 pts) — WHO 2024 priority list
    2. Antibiotic class importance (0-25 pts) — WHO CIA 6th revision
    3. Deviation magnitude         (0-30 pts) — forecast vs actual
    4. Geographic burden           (0-15 pts) — region + country context
    5. Impact weight               (0-10 pts) — NEW: mortality + scarcity

    Final score = sum, capped at 100.
    Tier: critical >= 70, warn >= 40, monitor < 40

Dependencies:
    None beyond standard library.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "severity_scorer.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("severity_scorer")


# ---------------------------------------------------------------------------
# Tier thresholds
# ---------------------------------------------------------------------------

CRITICAL_THRESHOLD = 70
WARN_THRESHOLD = 40


# ---------------------------------------------------------------------------
# WHO 2024 Bacterial Priority Pathogens List
# ---------------------------------------------------------------------------

CRITICAL_COMBINATIONS: set[tuple[str, str]] = {
    ("Acinetobacter spp.", "Carbapenems"),
    ("Klebsiella pneumoniae", "Carbapenems"),
    ("Klebsiella pneumoniae", "Cephalosporins (3rd gen)"),
    ("Enterococcus faecium", "Glycopeptides"),
    ("Staphylococcus aureus", "Penicillins"),
    ("Pseudomonas aeruginosa", "Carbapenems"),
    ("Escherichia coli", "Carbapenems"),
}

PATHOGEN_PRIORITY: dict[str, str] = {
    "Acinetobacter spp.": "critical",
    "Klebsiella pneumoniae": "critical",
    "Enterococcus faecium": "critical",
    "Staphylococcus aureus": "critical",
    "Pseudomonas aeruginosa": "high",
    "Escherichia coli": "high",
    "Streptococcus pneumoniae": "high",
    "Enterococcus faecalis": "medium",
}

PATHOGEN_PRIORITY_SCORES: dict[str, int] = {
    "critical": 30,
    "high": 20,
    "medium": 10,
}


# ---------------------------------------------------------------------------
# WHO Critically Important Antimicrobials (CIA) — 6th revision
# ---------------------------------------------------------------------------

ANTIBIOTIC_CLASS_SCORES: dict[str, int] = {
    "Carbapenems": 25,
    "Glycopeptides": 22,
    "Cephalosporins (3rd gen)": 20,
    "Cephalosporins (4th gen)": 20,
    "Beta-lactam combinations": 18,
    "Fluoroquinolones": 18,
    "Oxazolidinones": 18,
    "Polymyxins": 20,
    "Aminoglycosides": 12,
    "Penicillins": 12,
    "Macrolides": 8,
    "Tetracyclines": 6,
    "Lincosamides": 6,
    "Folate pathway inhibitors": 6,
    "Nitrofurans": 5,
    "Phosphonic acids": 5,
    "Amphenicols": 5,
}

DEFAULT_ANTIBIOTIC_SCORE = 5


# ---------------------------------------------------------------------------
# Geographic burden context
# ---------------------------------------------------------------------------

HIGH_BURDEN_REGIONS: set[str] = {"AFRO", "SEARO", "EMRO"}

HIGH_BURDEN_EURO_COUNTRIES: set[str] = {
    "BGR", "ROU", "GRC", "LTU", "CYP", "HRV", "ITA",
    "HUN", "SVK", "POL", "LVA", "RUS",
}


# ---------------------------------------------------------------------------
# Impact weight tables (NEW — Task 5.3)
# ---------------------------------------------------------------------------

# Attributable mortality weight per pathogen (0-4 scale)
# Source: GBD 2019 AMR Collaborators, Lancet 2022
# Scale: 4 = highest attributable mortality (CRAB, CRKP), 1 = lower
PATHOGEN_MORTALITY_WEIGHT: dict[str, int] = {
    "Acinetobacter spp.": 4,          # CRAB — highest attributable mortality
    "Klebsiella pneumoniae": 4,        # CRKP — second highest globally
    "Staphylococcus aureus": 3,        # MRSA — major ICU burden
    "Pseudomonas aeruginosa": 3,       # High ICU mortality
    "Escherichia coli": 2,             # High incidence, moderate mortality
    "Enterococcus faecium": 2,         # VRE — high mortality in immunocompromised
    "Streptococcus pneumoniae": 2,     # High community burden
    "Enterococcus faecalis": 1,        # Lower mortality than faecium
}

# Treatment alternatives remaining when this antibiotic class fails (0-4 scale)
# 4 = last resort (no alternatives), 0 = many alternatives exist
# Source: IDSA guidance + EUCAST treatment ladders
TREATMENT_SCARCITY: dict[str, int] = {
    "Carbapenems": 4,           # Last-resort — failure = very few options
    "Glycopeptides": 4,         # Last-resort for gram-positive (VRE/MRSA)
    "Polymyxins": 4,            # Last-resort for pan-resistant gram-negatives
    "Oxazolidinones": 3,        # Linezolid — limited alternatives
    "Cephalosporins (3rd gen)": 3,  # ESBL — limits to carbapenems
    "Cephalosporins (4th gen)": 3,
    "Beta-lactam combinations": 2,
    "Fluoroquinolones": 2,
    "Aminoglycosides": 1,
    "Penicillins": 1,
    "Macrolides": 1,
    "Tetracyclines": 1,
    "Lincosamides": 1,
    "Folate pathway inhibitors": 1,
    "Nitrofurans": 1,
    "Phosphonic acids": 1,
    "Amphenicols": 0,
}

# Hospital burden multiplier — pathogens primarily causing ICU/nosocomial infections
# score higher because outcomes are worse and spread is faster in healthcare settings
ICU_ASSOCIATED_PATHOGENS: set[str] = {
    "Acinetobacter spp.",
    "Klebsiella pneumoniae",
    "Pseudomonas aeruginosa",
    "Staphylococcus aureus",
    "Enterococcus faecium",
}


# ---------------------------------------------------------------------------
# Antibiotic name -> class lookup
# ---------------------------------------------------------------------------

ANTIBIOTIC_TO_CLASS: dict[str, str] = {
    "Imipenem": "Carbapenems",
    "Meropenem": "Carbapenems",
    "Ertapenem": "Carbapenems",
    "Vancomycin": "Glycopeptides",
    "Teicoplanin": "Glycopeptides",
    "Ceftriaxone": "Cephalosporins (3rd gen)",
    "Cefotaxime": "Cephalosporins (3rd gen)",
    "Ceftazidime": "Cephalosporins (3rd gen)",
    "Cefepime": "Cephalosporins (4th gen)",
    "Cefazolin": "Cephalosporins (1st gen)",
    "Ciprofloxacin": "Fluoroquinolones",
    "Levofloxacin": "Fluoroquinolones",
    "Gentamicin": "Aminoglycosides",
    "Gentamicin (high level)": "Aminoglycosides",
    "Amikacin": "Aminoglycosides",
    "Ampicillin": "Penicillins",
    "Oxacillin": "Penicillins",
    "Methicillin": "Penicillins",
    "Piperacillin-tazobactam": "Beta-lactam combinations",
    "Amoxicillin-clavulanic acid": "Beta-lactam combinations",
    "Azithromycin": "Macrolides",
    "Clarithromycin": "Macrolides",
    "Tetracycline": "Tetracyclines",
    "Doxycycline": "Tetracyclines",
    "Linezolid": "Oxazolidinones",
    "Clindamycin": "Lincosamides",
    "Trimethoprim-sulfamethoxazole": "Folate pathway inhibitors",
    "Colistin": "Polymyxins",
    "Polymyxin B": "Polymyxins",
    "Nitrofurantoin": "Nitrofurans",
    "Fosfomycin": "Phosphonic acids",
    "Chloramphenicol": "Amphenicols",
}


# ---------------------------------------------------------------------------
# Scored signal dataclass
# ---------------------------------------------------------------------------

@dataclass
class ScoredSignal:
    """AnomalySignal with severity score, tier, and full score breakdown."""

    # Original signal fields
    pathogen_name: str
    antibiotic_name: str
    country_iso3: str
    year: int
    signal_type: str
    current_resistance: float
    forecasted_rate: float
    deviation_magnitude: float
    trend_direction: str
    trend_slope: float
    cusum_value: float
    residuals_std: float
    region_who: str
    regional_mean: float
    data_source: str
    detected_at: datetime

    # Severity outputs
    severity_score: int = 0
    severity_tier: str = "monitor"

    # Score breakdown — includes impact dimension for explainability
    score_breakdown: dict = field(default_factory=dict)

    # Forecast confidence intervals — passed through from AnomalySignal (Task 5.5)
    forecast_lower_80: Optional[float] = None
    forecast_upper_80: Optional[float] = None
    forecast_lower_50: Optional[float] = None
    forecast_upper_50: Optional[float] = None


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------

def score_pathogen_priority(
    pathogen_name: str,
    antibiotic_class: Optional[str],
) -> tuple[int, str]:
    """Score based on WHO 2024 pathogen priority tier. Returns (score, tier)."""
    if antibiotic_class:
        combo = (pathogen_name, antibiotic_class)
        if combo in CRITICAL_COMBINATIONS:
            return PATHOGEN_PRIORITY_SCORES["critical"], "critical"

    tier = PATHOGEN_PRIORITY.get(pathogen_name, "medium")
    return PATHOGEN_PRIORITY_SCORES.get(tier, 10), tier


def score_antibiotic_importance(antibiotic_class: Optional[str]) -> int:
    """Score based on WHO critically important antimicrobial classification."""
    if not antibiotic_class:
        return DEFAULT_ANTIBIOTIC_SCORE
    return ANTIBIOTIC_CLASS_SCORES.get(antibiotic_class, DEFAULT_ANTIBIOTIC_SCORE)


def score_deviation_magnitude(
    deviation_magnitude: float,
    trend_direction: str,
    signal_type: str,
    forecasted_rate: float = -1.0,
) -> tuple[int, float]:
    """
    Score deviation from forecast (0-30 pts).

    Includes zero-forecast artefact guard: if TFT returned 0.0%,
    cap the deviation score at 18/30 to prevent artefact inflation.
    Falling trend penalty: 0.55x multiplier.
    """
    abs_dev = abs(deviation_magnitude)
    normalised = min(abs_dev / 0.30, 1.0)
    raw_score = normalised * 30.0

    if trend_direction == "rising":
        raw_score *= 1.20
    elif trend_direction == "falling":
        raw_score *= 0.55

    if signal_type == "rate_spike":
        raw_score *= 1.05

    # Zero-forecast artefact guard
    ZERO_FORECAST_THRESHOLD = 0.005
    if 0 <= forecasted_rate < ZERO_FORECAST_THRESHOLD:
        raw_score = min(raw_score, 18.0)

    score = min(int(round(raw_score)), 30)
    return score, normalised


def score_geographic_burden(
    region_who: str,
    country_iso3: str,
    regional_mean: float,
    current_resistance: float,
) -> int:
    """Score geographic burden context (0-15 pts)."""
    if region_who in HIGH_BURDEN_REGIONS:
        base_score = 15
    elif country_iso3 in HIGH_BURDEN_EURO_COUNTRIES:
        base_score = 10
    elif region_who == "EURO":
        base_score = 5
    else:
        base_score = 7

    if regional_mean > 0 and current_resistance > regional_mean * 1.5:
        base_score = min(base_score + 3, 15)
    if regional_mean > 0.40:
        base_score = min(base_score + 2, 15)

    return base_score


def score_absolute_resistance(current_resistance: float) -> int:
    """Absolute resistance bonus (0-5 pts) for score differentiation at top."""
    if current_resistance >= 0.60:
        return 5
    elif current_resistance >= 0.50:
        return 4
    elif current_resistance >= 0.40:
        return 3
    elif current_resistance >= 0.25:
        return 2
    elif current_resistance >= 0.10:
        return 1
    return 0


def score_impact_weight(
    pathogen_name: str,
    antibiotic_class: Optional[str],
) -> tuple[int, dict]:
    """
    Score clinical impact weight (0-10 pts) — NEW in Task 5.3.

    Combines:
        - Attributable mortality weight (0-4 pts)
        - Treatment scarcity (0-4 pts) — alternatives remaining on failure
        - Hospital/ICU burden multiplier (+2 pts for ICU-associated pathogens)

    This ensures the score reflects what happens to patients when this
    resistance event occurs — not just how unusual the resistance is.

    Args:
        pathogen_name: Canonical pathogen name.
        antibiotic_class: Antibiotic class, or None.

    Returns:
        Tuple of (score 0-10, breakdown dict).
    """
    mortality_pts = PATHOGEN_MORTALITY_WEIGHT.get(pathogen_name, 1)
    scarcity_pts = TREATMENT_SCARCITY.get(antibiotic_class, 1) if antibiotic_class else 1
    icu_bonus = 2 if pathogen_name in ICU_ASSOCIATED_PATHOGENS else 0

    # Cap: mortality (0-4) + scarcity (0-4) + ICU bonus (0-2) = max 10
    raw = mortality_pts + scarcity_pts + icu_bonus
    score = min(raw, 10)

    breakdown = {
        "mortality_weight": mortality_pts,
        "treatment_scarcity": scarcity_pts,
        "icu_bonus": icu_bonus,
        "total": score,
    }

    return score, breakdown


def resolve_antibiotic_class(antibiotic_name: str) -> Optional[str]:
    """Resolve antibiotic name to class string."""
    return ANTIBIOTIC_TO_CLASS.get(antibiotic_name)


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_signal(anomaly) -> ScoredSignal:
    """
    Score a single AnomalySignal — 5 dimensions + impact weight.

    Returns ScoredSignal with full breakdown for explainability.
    The breakdown dict is what powers the "Why?" panel in the dashboard.
    """
    antibiotic_class = resolve_antibiotic_class(anomaly.antibiotic_name)

    # Dimension 1: Pathogen priority
    pathogen_score, pathogen_tier = score_pathogen_priority(
        anomaly.pathogen_name, antibiotic_class
    )

    # Dimension 2: Antibiotic class importance
    antibiotic_score = score_antibiotic_importance(antibiotic_class)

    # Dimension 3: Deviation magnitude (with artefact guard)
    deviation_score, normalised_dev = score_deviation_magnitude(
        anomaly.deviation_magnitude,
        anomaly.trend_direction,
        anomaly.signal_type,
        forecasted_rate=getattr(anomaly, "forecasted_rate", -1.0),
    )

    # Dimension 4: Geographic burden
    geo_score = score_geographic_burden(
        anomaly.region_who,
        anomaly.country_iso3,
        anomaly.regional_mean,
        anomaly.current_resistance,
    )

    # Dimension 5: Impact weight (NEW)
    impact_score, impact_breakdown = score_impact_weight(
        anomaly.pathogen_name, antibiotic_class
    )

    # Absolute resistance bonus (score differentiation)
    abs_bonus = score_absolute_resistance(anomaly.current_resistance)

    # Total — capped at 100
    total = min(
        pathogen_score + antibiotic_score + deviation_score
        + geo_score + impact_score + abs_bonus,
        100,
    )

    # Tier classification
    if total >= CRITICAL_THRESHOLD:
        tier = "critical"
    elif total >= WARN_THRESHOLD:
        tier = "warn"
    else:
        tier = "monitor"

    # Falling trend override
    if anomaly.trend_direction == "falling" and tier == "critical":
        if anomaly.current_resistance < 0.65:
            tier = "warn"
            total = min(total, CRITICAL_THRESHOLD - 1)

    # Full breakdown for "Why?" explainability panel
    breakdown = {
        "pathogen_priority": pathogen_score,
        "pathogen_tier": pathogen_tier,
        "antibiotic_importance": antibiotic_score,
        "antibiotic_class": antibiotic_class or "unknown",
        "deviation_magnitude": deviation_score,
        "normalised_deviation": round(normalised_dev, 3),
        "geographic_burden": geo_score,
        "impact_weight": impact_score,
        "impact_detail": impact_breakdown,
        "absolute_resistance_bonus": abs_bonus,
        "total": total,
        "tier": tier,
        # Human-readable driver phrases for UI
        "drivers": _build_driver_phrases(
            pathogen_name=anomaly.pathogen_name,
            antibiotic_class=antibiotic_class,
            pathogen_tier=pathogen_tier,
            trend_direction=anomaly.trend_direction,
            current_resistance=anomaly.current_resistance,
            deviation_magnitude=anomaly.deviation_magnitude,
            impact_breakdown=impact_breakdown,
            region_who=anomaly.region_who,
        ),
    }

    return ScoredSignal(
        pathogen_name=anomaly.pathogen_name,
        antibiotic_name=anomaly.antibiotic_name,
        country_iso3=anomaly.country_iso3,
        year=anomaly.year,
        signal_type=anomaly.signal_type,
        current_resistance=anomaly.current_resistance,
        forecasted_rate=anomaly.forecasted_rate,
        deviation_magnitude=anomaly.deviation_magnitude,
        trend_direction=anomaly.trend_direction,
        trend_slope=anomaly.trend_slope,
        cusum_value=anomaly.cusum_value,
        residuals_std=anomaly.residuals_std,
        region_who=anomaly.region_who,
        regional_mean=anomaly.regional_mean,
        data_source=anomaly.data_source,
        detected_at=anomaly.detected_at,
        severity_score=total,
        severity_tier=tier,
        score_breakdown=breakdown,
        forecast_lower_80=getattr(anomaly, "forecast_lower_80", None),
        forecast_upper_80=getattr(anomaly, "forecast_upper_80", None),
        forecast_lower_50=getattr(anomaly, "forecast_lower_50", None),
        forecast_upper_50=getattr(anomaly, "forecast_upper_50", None),
    )


def _build_driver_phrases(
    pathogen_name: str,
    antibiotic_class: Optional[str],
    pathogen_tier: str,
    trend_direction: str,
    current_resistance: float,
    deviation_magnitude: float,
    impact_breakdown: dict,
    region_who: str,
) -> list[str]:
    """
    Build human-readable driver phrases for the 'Why?' panel.

    Returns a list of short strings explaining why this signal scored
    as it did — e.g. ["WHO Priority 1 pathogen", "Last-resort antibiotic"].
    These are surfaced in the dashboard detail panel.
    """
    drivers = []

    # Pathogen
    if pathogen_tier == "critical":
        drivers.append(f"WHO Priority 1 pathogen")
    elif pathogen_tier == "high":
        drivers.append(f"WHO Priority 2 pathogen")

    # Antibiotic
    if antibiotic_class == "Carbapenems":
        drivers.append("Last-resort antibiotic class (carbapenem)")
    elif antibiotic_class == "Glycopeptides":
        drivers.append("Last-resort antibiotic class (glycopeptide/vancomycin)")
    elif antibiotic_class == "Polymyxins":
        drivers.append("Last-resort antibiotic class (polymyxin/colistin)")
    elif impact_breakdown.get("treatment_scarcity", 0) >= 3:
        drivers.append(f"High treatment scarcity ({antibiotic_class})")

    # Resistance level
    if current_resistance >= 0.60:
        drivers.append(f"Critically high resistance ({current_resistance:.1%})")
    elif current_resistance >= 0.40:
        drivers.append(f"High resistance rate ({current_resistance:.1%})")

    # Deviation
    if abs(deviation_magnitude) >= 0.40:
        drivers.append(f"Extreme deviation from forecast (+{deviation_magnitude:.1%})")
    elif abs(deviation_magnitude) >= 0.20:
        drivers.append(f"Large deviation from forecast (+{deviation_magnitude:.1%})")

    # Trend
    if trend_direction == "rising":
        drivers.append("Rising multi-year trend")
    elif trend_direction == "falling":
        drivers.append("Falling trend (situation may be improving)")

    # Impact
    if impact_breakdown.get("icu_bonus", 0) > 0:
        drivers.append("ICU-associated pathogen — higher hospital burden")

    if impact_breakdown.get("mortality_weight", 0) >= 4:
        drivers.append("High attributable mortality (GBD 2019 data)")

    # Geography
    if region_who in HIGH_BURDEN_REGIONS:
        drivers.append(f"High-burden WHO region ({region_who})")

    return drivers


def score_signals(anomalies: list) -> list[ScoredSignal]:
    """Score a list of AnomalySignal objects. Returns sorted by score descending."""
    scored = [score_signal(a) for a in anomalies]
    scored.sort(key=lambda s: s.severity_score, reverse=True)

    tier_counts = {
        "critical": sum(1 for s in scored if s.severity_tier == "critical"),
        "warn": sum(1 for s in scored if s.severity_tier == "warn"),
        "monitor": sum(1 for s in scored if s.severity_tier == "monitor"),
    }

    logger.info(
        "Scored %d signals. Critical: %d | Warn: %d | Monitor: %d",
        len(scored), tier_counts["critical"],
        tier_counts["warn"], tier_counts["monitor"],
    )

    return scored


# ---------------------------------------------------------------------------
# Full pipeline entry point
# ---------------------------------------------------------------------------

def run_severity_scoring() -> list[ScoredSignal]:
    """Run the full severity scoring pipeline end to end."""
    started_at = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("Severity scoring started: %s", started_at.isoformat())

    from amr_sentinel.models.anomaly_detector import run_anomaly_detection
    anomalies = run_anomaly_detection(dry_run=True, write_to_db=False)

    if not anomalies:
        logger.warning("No anomalies to score.")
        return []

    scored = score_signals(anomalies)

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    logger.info("-" * 60)
    logger.info("Severity scoring complete in %.1fs", elapsed)
    logger.info(
        "  Scored %d signals. Top score: %d (%s)",
        len(scored), scored[0].severity_score, scored[0].severity_tier,
    )
    logger.info("=" * 60)

    return scored


# ---------------------------------------------------------------------------
# Standalone usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from amr_sentinel.models.anomaly_detector import run_anomaly_detection

    logger.info("Running anomaly detection (dry run) before scoring...")
    anomalies = run_anomaly_detection(dry_run=True, write_to_db=False)

    if not anomalies:
        print("No anomalies detected.")
        sys.exit(0)

    scored = score_signals(anomalies)

    critical = [s for s in scored if s.severity_tier == "critical"]
    warn = [s for s in scored if s.severity_tier == "warn"]
    monitor = [s for s in scored if s.severity_tier == "monitor"]

    print(f"\nSeverity scoring complete. Total: {len(scored)}")
    print(f"  Critical: {len(critical)}  Warn: {len(warn)}  Monitor: {len(monitor)}")

    print(f"\nTop 15 signals:")
    print(f"{'Score':>6} {'Tier':<10} {'Pathogen':<25} {'Antibiotic':<28} "
          f"{'Country':<8} {'Actual':>8} {'Impact':>7}")
    print("-" * 100)
    for s in scored[:15]:
        bd = s.score_breakdown
        print(
            f"{s.severity_score:>6} {s.severity_tier:<10} "
            f"{s.pathogen_name:<25} {s.antibiotic_name:<28} "
            f"{s.country_iso3:<8} {s.current_resistance:>8.1%} "
            f"{bd.get('impact_weight', 0):>7}/10"
        )

    if critical:
        top = critical[0]
        bd = top.score_breakdown
        print(f"\nTop signal 'Why?' drivers:")
        for driver in bd.get("drivers", []):
            print(f"  ✓ {driver}")
        print(f"\nImpact breakdown:")
        imp = bd.get("impact_detail", {})
        print(f"  Mortality weight:     {imp.get('mortality_weight', 0)}/4")
        print(f"  Treatment scarcity:   {imp.get('treatment_scarcity', 0)}/4")
        print(f"  ICU burden bonus:     {imp.get('icu_bonus', 0)}/2")
        print(f"  Total impact score:   {bd.get('impact_weight', 0)}/10")