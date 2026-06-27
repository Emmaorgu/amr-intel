"""
Genomic Precursor Detector v2
==============================
Cross-references genomic_signals (NCBI NDARO) against resistance_records
(WHO/ECDC/NGA phenotypic surveillance) to surface pre-phenotypic emergence signals.

A genomic precursor signal fires when a priority resistance gene is growing in
clinical isolates in a country where phenotypic surveillance data is absent or low.

Key design decisions:
- Multi-year acceleration scoring using exponential fit (not single YoY jump)
- Surveillance gap honesty: distinguishes countries where absence of phenotypic
  data reflects a genuine gap vs countries with independent surveillance not yet
  ingested (USA, China, etc.)
- Doubling time estimation and days-to-threshold projection
- Deduplication: one signal per gene/pathogen/country (most recent year window)
- Geographic linkage notes for epidemiologically connected countries

Signal type: 'genomic_precursor'

Inputs:
    - PostgreSQL amr_sentinel database (DB_* env vars)
    - genomic_signals table (NCBI NDARO, 16,882 signals)
    - resistance_records table (WHO/ECDC/NGA, 7,719 records)

Outputs:
    - List of PrecursorSignal objects
    - JSON report at data/genomic/precursor_signals.json

Dependencies:
    pip install sqlalchemy psycopg2-binary python-dotenv numpy
"""

import json
import logging
import math
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("genomic_precursor_detector")

DATABASE_URL: str = (
    f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)

OUTPUT_DIR = Path("data/genomic")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH = OUTPUT_DIR / "precursor_signals.json"

MIN_ISOLATE_COUNT_LATEST = 10
MIN_SIGNAL_YEAR = 2018
PHENOTYPIC_VERY_LOW = 0.05
PHENOTYPIC_LOW = 0.10
FORECAST_THRESHOLD = 500

# ---------------------------------------------------------------------------
# Gene metadata
# ---------------------------------------------------------------------------

GENE_TO_PHENOTYPE: dict[str, dict] = {
    "NDM":    {"drug_class": "Carbapenem",               "antibiotics": ["Meropenem", "Imipenem"],                         "tier": 1, "who": "CRITICAL", "desc": "New Delhi Metallo-beta-lactamase — pan-carbapenem resistance"},
    "OXA-48": {"drug_class": "Carbapenem",               "antibiotics": ["Meropenem", "Imipenem"],                         "tier": 1, "who": "CRITICAL", "desc": "OXA-48 carbapenemase — widespread in Enterobacteriaceae"},
    "OXA-23": {"drug_class": "Carbapenem",               "antibiotics": ["Meropenem", "Imipenem"],                         "tier": 1, "who": "CRITICAL", "desc": "OXA-23 carbapenemase — dominant in Acinetobacter baumannii"},
    "KPC":    {"drug_class": "Carbapenem",               "antibiotics": ["Meropenem", "Imipenem", "Ertapenem"],            "tier": 1, "who": "CRITICAL", "desc": "KPC carbapenemase — globally dominant, plasmid-spread"},
    "VIM":    {"drug_class": "Carbapenem",               "antibiotics": ["Meropenem", "Imipenem"],                         "tier": 1, "who": "CRITICAL", "desc": "VIM Metallo-beta-lactamase"},
    "IMP":    {"drug_class": "Carbapenem",               "antibiotics": ["Meropenem", "Imipenem"],                         "tier": 1, "who": "CRITICAL", "desc": "Imipenemase Metallo-beta-lactamase"},
    "MCR":    {"drug_class": "Colistin",                 "antibiotics": ["Colistin"],                                      "tier": 1, "who": "CRITICAL", "desc": "Mobilised Colistin Resistance — plasmid-mediated, last-resort antibiotic"},
    "CTX-M":  {"drug_class": "3rd-gen cephalosporin",   "antibiotics": ["Ceftriaxone", "Ceftazidime"],                    "tier": 2, "who": "HIGH",     "desc": "CTX-M ESBL — most common extended-spectrum beta-lactamase globally"},
    "mecA":   {"drug_class": "Methicillin",              "antibiotics": ["Methicillin", "Oxacillin"],                      "tier": 2, "who": "HIGH",     "desc": "mecA — MRSA determinant"},
    "mecC":   {"drug_class": "Methicillin",              "antibiotics": ["Methicillin"],                                   "tier": 2, "who": "HIGH",     "desc": "mecC — alternative MRSA mechanism, zoonotic origin"},
    "VAN":    {"drug_class": "Vancomycin",               "antibiotics": ["Vancomycin"],                                    "tier": 2, "who": "HIGH",     "desc": "Vancomycin resistance (vanA/vanB) — VRE determinants"},
}

# ---------------------------------------------------------------------------
# Country surveillance classification
# ---------------------------------------------------------------------------

# Countries with strong independent AMR surveillance not in our sources.
# "No phenotypic data" here = data source gap, not confirmed pre-phenotypic state.
SURVEILLANCE_GAP_COUNTRIES = {"USA", "CHN", "AUS", "CAN", "JPN", "KOR", "NZL"}

# ECDC EARS-Net countries — excellent phenotypic coverage ingested.
ECDC_COUNTRIES = {
    "AUT", "BEL", "BGR", "HRV", "CYP", "CZE", "DNK", "EST", "FIN", "FRA",
    "DEU", "GRC", "HUN", "ISL", "IRL", "ITA", "LVA", "LTU", "LUX", "MLT",
    "NLD", "NOR", "POL", "PRT", "ROU", "SVK", "SVN", "ESP", "SWE", "GBR",
}

# Epidemiological linkages — countries this signal is likely to spread to
EPIGEOGRAPHIC_LINKS: dict[str, list[str]] = {
    "IND": ["GBR", "NGA", "ZAF", "BGD", "PAK"],
    "BGD": ["GBR", "IND", "SAU"],
    "NGA": ["GBR", "USA", "ZAF", "GHA"],
    "ZAF": ["GBR", "ZWE", "MOZ", "NGA"],
    "THA": ["GBR", "AUS", "SGP"],
    "PAK": ["GBR", "SAU", "UAE"],
    "EGY": ["SAU", "ITA", "GRC"],
    "CHN": ["USA", "AUS", "GBR"],
}

# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class PrecursorSignal:
    """A pre-phenotypic resistance emergence signal."""
    gene_name: str
    gene_family: str
    pathogen_name: str
    country_iso3: str
    region_who: Optional[str]

    latest_year: int
    latest_isolate_count: int
    time_series: dict
    acceleration_score: float
    doubling_time_years: Optional[float]
    days_to_threshold: Optional[int]

    phenotypic_rate: Optional[float]
    phenotypic_gap: str
    phenotypic_source: Optional[str]

    surveillance_confidence: str
    surveillance_caveat: str

    severity_score: int
    drug_class: str
    who_priority: str
    gene_description: str
    signal_type: str = "genomic_precursor"
    spread_risk_countries: list = field(default_factory=list)
    intelligence_summary: str = ""
    detected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

ALL_YEARS_QUERY = text("""
    SELECT
        gs.gene_name,
        gs.gene_family,
        gs.pathogen_name,
        gs.country_iso3,
        gs.region_who,
        gs.year,
        gs.isolate_count
    FROM genomic_signals gs
    WHERE gs.isolate_count >= 3
    ORDER BY gs.gene_name, gs.pathogen_name, gs.country_iso3, gs.year
""")

PHENOTYPIC_QUERY = text("""
    SELECT rr.resistance_rate, rr.data_source, rr.year
    FROM resistance_records rr
    WHERE rr.pathogen_name ILIKE :pathogen_pattern
      AND rr.antibiotic_name ILIKE :antibiotic_pattern
      AND rr.country_iso3 = :country_iso3
      AND rr.year >= 2015
    ORDER BY rr.year DESC
    LIMIT 5
""")


def fetch_all_time_series(session) -> dict[tuple, dict]:
    """
    Fetch complete year-by-year isolate counts for every gene/pathogen/country triplet.

    Returns:
        Dict mapping (gene_name, gene_family, pathogen_name, country_iso3, region_who)
        to {year: isolate_count}
    """
    result = session.execute(ALL_YEARS_QUERY)
    triplets: dict[tuple, dict] = {}
    for row in result:
        key = (row.gene_name, row.gene_family, row.pathogen_name,
               row.country_iso3, row.region_who)
        if key not in triplets:
            triplets[key] = {}
        triplets[key][row.year] = row.isolate_count
    logger.info("Loaded %d unique gene/pathogen/country triplets", len(triplets))
    return triplets


def fetch_phenotypic_context(
    session,
    pathogen_name: str,
    antibiotics: list[str],
    country_iso3: str,
) -> Optional[tuple[float, str]]:
    """Look up most recent phenotypic resistance rate for pathogen/country."""
    genus = pathogen_name.split()[0]
    for antibiotic in antibiotics:
        result = session.execute(PHENOTYPIC_QUERY, {
            "pathogen_pattern": f"%{genus}%",
            "antibiotic_pattern": f"%{antibiotic}%",
            "country_iso3": country_iso3,
        })
        for row in result:
            if row.resistance_rate is not None:
                return float(row.resistance_rate), row.data_source
    return None


# ---------------------------------------------------------------------------
# Acceleration analysis
# ---------------------------------------------------------------------------


def compute_acceleration(time_series: dict[int, int]) -> dict:
    """
    Fit exponential growth model to isolate count time series.
    Returns growth rate, doubling time, acceleration score, and forecast.

    For exponential model y = a * exp(r * t):
        r = growth rate per year
        doubling_time = ln(2) / r
        acceleration_score = normalised 0-1 from doubling speed x R²

    Args:
        time_series: {year: isolate_count}

    Returns:
        Dict with growth_rate, doubling_time_years, acceleration_score,
        days_to_threshold, r_squared, trend
    """
    result = {
        "growth_rate": None,
        "doubling_time_years": None,
        "acceleration_score": 0.0,
        "days_to_threshold": None,
        "r_squared": None,
        "trend": "insufficient_data",
    }

    years = sorted(time_series.keys())
    counts = [time_series[y] for y in years]

    if len(years) < 2:
        return result

    log_counts = []
    valid_years = []
    for y, c in zip(years, counts):
        if c > 0:
            log_counts.append(math.log(c))
            valid_years.append(y)

    if len(valid_years) < 2:
        return result

    x = np.array(valid_years, dtype=float)
    y = np.array(log_counts, dtype=float)
    x_norm = x - x[0]

    try:
        coeffs = np.polyfit(x_norm, y, 1)
        r = float(coeffs[0])

        y_pred = np.polyval(coeffs, x_norm)
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_sq = float(1 - (ss_res / ss_tot)) if ss_tot > 0 else 0.0

        result["growth_rate"] = r
        result["r_squared"] = r_sq

        if r > 0.01:
            result["trend"] = "growing"
            doubling = math.log(2) / r
            result["doubling_time_years"] = round(doubling, 1)

            latest_count = counts[-1]
            if latest_count < FORECAST_THRESHOLD and r > 0:
                years_to_threshold = math.log(FORECAST_THRESHOLD / max(latest_count, 1)) / r
                result["days_to_threshold"] = int(years_to_threshold * 365)

            # Acceleration score: fast doubling + high R² = high score
            doubling_score = max(0.0, 1.0 - (doubling - 0.5) / 5.0)
            fit_quality = max(0.0, r_sq)
            result["acceleration_score"] = round(min(1.0, doubling_score * fit_quality), 3)

        elif r < -0.1:
            result["trend"] = "declining"
        else:
            result["trend"] = "stable"
            result["acceleration_score"] = 0.1

    except (np.linalg.LinAlgError, ValueError, ZeroDivisionError):
        result["trend"] = "insufficient_data"

    return result


# ---------------------------------------------------------------------------
# Scoring and classification
# ---------------------------------------------------------------------------


def classify_gap(rate: Optional[float]) -> str:
    """Classify phenotypic gap: absent / very_low / low / established."""
    if rate is None:
        return "absent"
    if rate <= PHENOTYPIC_VERY_LOW:
        return "very_low"
    if rate <= PHENOTYPIC_LOW:
        return "low"
    return "established"


def classify_confidence(country_iso3: str, gap: str) -> tuple[str, str]:
    """Return (confidence_label, caveat_text) based on surveillance coverage."""
    if country_iso3 in SURVEILLANCE_GAP_COUNTRIES and gap == "absent":
        return (
            "LOW",
            f"{country_iso3} has independent AMR surveillance (CDC/NHSN, China AMR) "
            f"not yet ingested by AMR-Sentinel. Absence of phenotypic data here reflects "
            f"a source gap, not confirmed pre-phenotypic status. Validate against national reports."
        )
    if country_iso3 in ECDC_COUNTRIES:
        if gap == "absent":
            return (
                "HIGH",
                f"{country_iso3} is covered by ECDC EARS-Net (ingested). "
                f"Gene presence without phenotypic signal is a genuine surveillance gap."
            )
        return ("HIGH", "")
    if gap in ("very_low", "low"):
        return (
            "HIGH",
            f"Phenotypic resistance is low ({gap.replace('_', ' ')}) — "
            f"genomic spread suggests rates may rise before next reporting cycle."
        )
    return (
        "MEDIUM",
        f"{country_iso3} has limited phenotypic surveillance coverage globally. "
        f"Genomic detection without phenotypic data is plausible as a true "
        f"pre-phenotypic signal but requires in-country validation."
    )


def score_signal(
    gene_tier: int,
    isolate_count: int,
    phenotypic_gap: str,
    acceleration: dict,
    surveillance_confidence: str,
    country_iso3: str = "",
) -> int:
    """
    Score a precursor signal 0-100.

    Components:
        Gene tier         (30 pts): tier 1 = 30, tier 2 = 15
        Phenotypic gap    (25 pts): absent=25, very_low=18, low=10
        Isolate count     (20 pts): log-scaled, saturates at ~1000
        Acceleration      (15 pts): fast exponential growth = 15
        Confidence        (10 pts): HIGH=10, MEDIUM=6, LOW=2
        Convergence bonus (10 pts): ECDC country + tier-1 gene + phenotypic already
                                    showing (low/very_low) + fast doubling (<2yr).
                                    This is the highest-value signal type: gene
                                    spreading in a well-surveilled country AND
                                    phenotypic resistance already beginning to appear.
                                    Imminent crisis — warrants CRITICAL tier.
    """
    tier_score = 30 if gene_tier == 1 else 15
    gap_score = {"absent": 25, "very_low": 18, "low": 10}.get(phenotypic_gap, 0)
    count_score = min(20, int(math.log10(max(isolate_count, 1)) * 5))
    accel_score = int(acceleration.get("acceleration_score", 0) * 15)
    conf_score = {"HIGH": 10, "MEDIUM": 6, "LOW": 2}.get(surveillance_confidence, 4)

    # Convergence bonus: ECDC country + critical gene + phenotype appearing + fast growth
    # This is the genuine "gene spreading before phenotype fully establishes" scenario —
    # the most actionable early warning signal in the system.
    convergence_bonus = 0
    doubling = acceleration.get("doubling_time_years")
    is_ecdc = country_iso3 in ECDC_COUNTRIES
    phenotype_emerging = phenotypic_gap in ("very_low", "low")
    fast_doubling = doubling is not None and doubling <= 2.0
    if is_ecdc and gene_tier == 1 and phenotype_emerging:
        convergence_bonus = 8   # ECDC + critical gene + phenotype starting = high value
        if fast_doubling:
            convergence_bonus = 12  # fast doubling makes it urgent

    return min(100, tier_score + gap_score + count_score + accel_score + conf_score + convergence_bonus)


def build_summary(signal: PrecursorSignal) -> str:
    """Build one-paragraph intelligence narrative for the signal."""
    ts = signal.time_series
    years_str = ", ".join(f"{y}: {ts[y]:,}" for y in sorted(ts.keys())[-4:])

    accel_str = ""
    if signal.doubling_time_years:
        accel_str = (
            f" The isolate count is doubling approximately every "
            f"{signal.doubling_time_years:.1f} year(s)."
        )

    threshold_str = ""
    if signal.days_to_threshold:
        est = datetime.now(timezone.utc) + timedelta(days=signal.days_to_threshold)
        threshold_str = (
            f" At current trajectory, isolate counts are projected to reach "
            f"{FORECAST_THRESHOLD:,} by approximately {est.strftime('%B %Y')}."
        )

    phenotypic_str = (
        f"Phenotypic {signal.drug_class.lower()} resistance in {signal.country_iso3}: "
        + (f"{signal.phenotypic_rate * 100:.1f}% ({signal.phenotypic_source})."
           if signal.phenotypic_rate is not None
           else "no data in AMR-Sentinel sources.")
    )

    spread_str = ""
    if signal.spread_risk_countries:
        spread_str = (
            f" Epidemiological linkages suggest spread risk to: "
            f"{', '.join(signal.spread_risk_countries)}."
        )

    caveat_str = f" {signal.surveillance_caveat}" if signal.surveillance_caveat else ""

    return (
        f"GENOMIC PRECURSOR [{signal.surveillance_confidence} confidence]: "
        f"{signal.gene_name} detected in {signal.pathogen_name} isolates in "
        f"{signal.country_iso3}. Recent trajectory (isolates/year): {years_str}."
        f"{accel_str}{threshold_str} "
        f"{phenotypic_str}{spread_str} "
        f"{signal.gene_description}.{caveat_str}"
    ).strip()


# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------


def run_detector(
    min_score: int = 40,
    top_n: Optional[int] = None,
    focus_countries: Optional[set] = None,
) -> list[PrecursorSignal]:
    """
    Full detection pipeline.

    Queries both genomic_signals and resistance_records, computes multi-year
    acceleration, classifies phenotypic gaps, scores signals, and returns
    ranked PrecursorSignal objects.

    Args:
        min_score: Minimum score to include (default 40)
        top_n: Return only top N signals
        focus_countries: If set, only return signals for these ISO3 codes

    Returns:
        Ranked list of PrecursorSignal objects, sorted by severity_score desc
    """
    logger.info("=== Genomic Precursor Detector v2 starting ===")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    SessionFactory = sessionmaker(bind=engine)

    signals: list[PrecursorSignal] = []
    skipped_established = 0
    skipped_declining = 0
    skipped_score = 0

    with SessionFactory() as session:
        triplets = fetch_all_time_series(session)

        for key, time_series in triplets.items():
            gene_name, gene_family, pathogen_name, country_iso3, region_who = key

            if focus_countries and country_iso3 not in focus_countries:
                continue

            gene_info = GENE_TO_PHENOTYPE.get(gene_family)
            if not gene_info:
                continue

            recent_series = {y: c for y, c in time_series.items() if y >= MIN_SIGNAL_YEAR}
            if not recent_series:
                continue

            latest_year = max(recent_series.keys())
            latest_count = recent_series[latest_year]

            if latest_count < MIN_ISOLATE_COUNT_LATEST:
                continue

            acceleration = compute_acceleration(time_series)

            if acceleration["trend"] == "declining":
                skipped_declining += 1
                continue

            phenotypic_result = fetch_phenotypic_context(
                session, pathogen_name, gene_info["antibiotics"], country_iso3
            )
            phenotypic_rate = phenotypic_result[0] if phenotypic_result else None
            phenotypic_source = phenotypic_result[1] if phenotypic_result else None
            gap = classify_gap(phenotypic_rate)

            if gap == "established":
                skipped_established += 1
                continue

            confidence, caveat = classify_confidence(country_iso3, gap)

            score = score_signal(
                gene_info["tier"], latest_count, gap, acceleration, confidence,
                country_iso3=country_iso3,
            )

            if score < min_score:
                skipped_score += 1
                continue

            spread_countries = EPIGEOGRAPHIC_LINKS.get(country_iso3, [])

            signal = PrecursorSignal(
                gene_name=gene_name,
                gene_family=gene_family,
                pathogen_name=pathogen_name,
                country_iso3=country_iso3,
                region_who=region_who,
                latest_year=latest_year,
                latest_isolate_count=latest_count,
                time_series=recent_series,
                acceleration_score=acceleration["acceleration_score"],
                doubling_time_years=acceleration["doubling_time_years"],
                days_to_threshold=acceleration["days_to_threshold"],
                phenotypic_rate=phenotypic_rate,
                phenotypic_gap=gap,
                phenotypic_source=phenotypic_source,
                surveillance_confidence=confidence,
                surveillance_caveat=caveat,
                severity_score=score,
                drug_class=gene_info["drug_class"],
                who_priority=gene_info["who"],
                gene_description=gene_info["desc"],
                spread_risk_countries=spread_countries,
            )
            signal.intelligence_summary = build_summary(signal)
            signals.append(signal)

    signals.sort(key=lambda s: (s.severity_score, s.latest_isolate_count), reverse=True)

    if top_n:
        signals = signals[:top_n]

    logger.info(
        "Complete: %d signals (score>=%d) | %d established skipped | "
        "%d declining skipped | %d below score threshold",
        len(signals), min_score, skipped_established, skipped_declining, skipped_score,
    )

    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "version": 2,
        "min_score": min_score,
        "total_signals": len(signals),
        "skipped_established": skipped_established,
        "skipped_declining": skipped_declining,
        "skipped_below_score": skipped_score,
        "signals": [asdict(s) for s in signals],
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info("Report saved to %s", REPORT_PATH)

    return signals


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def print_summary(signals: list[PrecursorSignal]) -> None:
    """Print readable summary of precursor signals."""
    conf_icon = {"HIGH": "●", "MEDIUM": "◐", "LOW": "○"}

    print(f"\n{'='*75}")
    print(f"GENOMIC PRECURSOR SIGNALS  —  {len(signals)} detected")
    print("  ● HIGH confidence   ◐ MEDIUM (limited surveillance)   ○ LOW (source gap)")
    print(f"{'='*75}")

    for s in signals:
        icon = conf_icon.get(s.surveillance_confidence, "?")
        doubling = f"  2x/{s.doubling_time_years:.1f}yr" if s.doubling_time_years else ""
        threshold = f"  →{FORECAST_THRESHOLD} in {s.days_to_threshold}d" if s.days_to_threshold else ""

        ts_sorted = sorted(s.time_series.items())[-4:]
        spark = "  ".join(f"{y}: {c:,}" for y, c in ts_sorted)

        pheno = (
            f"phenotypic {s.phenotypic_rate*100:.0f}%"
            if s.phenotypic_rate is not None
            else "NO PHENOTYPIC DATA"
        )
        spread = f"  → spread risk: {', '.join(s.spread_risk_countries)}" if s.spread_risk_countries else ""

        print(
            f"\n  [{s.severity_score:3d}] {icon} {s.gene_name:<18} "
            f"{s.pathogen_name[:24]:<24} {s.country_iso3}{doubling}{threshold}"
        )
        print(f"        Trajectory: {spark}")
        print(f"        {pheno}{spread}")

    print(f"\n  Full report: {REPORT_PATH}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Genomic Precursor Detector v2 — multi-year acceleration scoring"
    )
    parser.add_argument("--min-score", type=int, default=40,
                        help="Minimum score threshold (default: 40)")
    parser.add_argument("--top", type=int, default=None,
                        help="Show only top N signals")
    parser.add_argument("--high-confidence-only", action="store_true",
                        help="Show only HIGH confidence signals")
    parser.add_argument("--country", type=str, default=None,
                        help="Filter to single country ISO3 e.g. NGA")
    args = parser.parse_args()

    focus = {args.country} if args.country else None

    if args.high_confidence_only:
        # Run without top_n to avoid cutting HIGH confidence signals
        signals = run_detector(min_score=args.min_score, focus_countries=focus)
        signals = [s for s in signals if s.surveillance_confidence == "HIGH"]
        if args.top:
            signals = signals[:args.top]
    else:
        signals = run_detector(
            min_score=args.min_score,
            top_n=args.top,
            focus_countries=focus,
        )

    print_summary(signals)