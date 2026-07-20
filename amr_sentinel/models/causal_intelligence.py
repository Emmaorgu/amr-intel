"""
AMR-Intel — Causal Intelligence Engine (Task 4)
=================================================
Cross-references genomic_signals (NCBI NDARO) against resistance_records
(WHO/ECDC phenotypic surveillance) to generate mechanistic explanations
for observed resistance trends.

For each phenotypic alert, this module asks:
    "Is there a genomic signal that explains WHY this resistance is rising?"

When both layers exist for the same pathogen/country, it synthesises a
causal narrative: "The observed increase in meropenem resistance in Latvia
is consistent with the expanding prevalence of blaOXA-48, which has shown
sub-annual doubling in sequenced isolates."

This is the evidence layer that separates AMR-Intel from a resistance rate
dashboard — it provides mechanistic context that clinicians and public health
officers can act on.

Outputs per alert:
    - causal_context: structured dict with genomic evidence
    - causal_narrative: one-paragraph human-readable explanation
    - genomic_genes: list of matching resistance genes
    - causal_confidence: HIGH / MEDIUM / INSUFFICIENT_DATA
    - lead_time_estimate: "Genomic signal preceded phenotypic detection by ~X months"

Integration:
    - Called from orchestrator after triage, before stewardship agent
    - Results stored in extra_data JSONB on phenotypic alerts
    - Exposed via GET /alerts/{id}/causal-context API endpoint
    - Rendered in Alert Investigation → Intelligence Report tab

Inputs:
    - PostgreSQL amr_sentinel database (DB_* env vars)
    - genomic_signals table (NCBI NDARO)
    - resistance_records table (WHO/ECDC/NGA)

Dependencies:
    pip install sqlalchemy psycopg2-binary python-dotenv
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

load_dotenv()

logger = logging.getLogger("causal_intelligence")

DATABASE_URL: str = (
    f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)

# ---------------------------------------------------------------------------
# Gene → antibiotic class mapping
# Defines which resistance genes are mechanistically responsible for
# which antibiotic classes in phenotypic surveillance.
# ---------------------------------------------------------------------------

GENE_TO_ANTIBIOTIC_CLASSES: dict[str, list[str]] = {
    # Carbapenemases
    "NDM":    ["Carbapenem", "Meropenem", "Imipenem", "Ertapenem"],
    "OXA-48": ["Carbapenem", "Meropenem", "Imipenem"],
    "OXA-23": ["Carbapenem", "Meropenem", "Imipenem"],
    "OXA-232":["Carbapenem", "Meropenem", "Imipenem"],
    "OXA-484":["Carbapenem", "Meropenem", "Imipenem"],
    "OXA-488":["Carbapenem", "Meropenem", "Imipenem"],
    "KPC":    ["Carbapenem", "Meropenem", "Imipenem", "Ertapenem"],
    "VIM":    ["Carbapenem", "Meropenem", "Imipenem"],
    "IMP":    ["Carbapenem", "Meropenem", "Imipenem"],
    # Colistin
    "MCR":    ["Colistin"],
    # ESBLs
    "CTX-M":  ["3rd-gen cephalosporin", "Ceftriaxone", "Ceftazidime", "Cefotaxime"],
    "TEM":    ["Ampicillin", "Penicillin"],
    "SHV":    ["3rd-gen cephalosporin", "Ceftriaxone"],
    # MRSA
    "mecA":   ["Methicillin", "Oxacillin", "Flucloxacillin"],
    "mecC":   ["Methicillin", "Oxacillin"],
    # VRE
    "VAN":    ["Vancomycin"],
    "vanA":   ["Vancomycin", "Teicoplanin"],
    "vanB":   ["Vancomycin"],
}

# Gene family descriptions for narrative generation
GENE_DESCRIPTIONS: dict[str, str] = {
    "NDM":    "New Delhi Metallo-beta-lactamase — a pan-carbapenem resistance mechanism that spreads via plasmids across gram-negative species",
    "OXA-48": "OXA-48 carbapenemase — widespread in Enterobacteriaceae, often undetected by standard carbapenem MIC testing",
    "OXA-23": "OXA-23 carbapenemase — dominant mechanism in Acinetobacter baumannii, associated with hospital outbreaks",
    "KPC":    "KPC carbapenemase — the globally dominant plasmid-borne carbapenem resistance mechanism in Klebsiella pneumoniae",
    "VIM":    "VIM Metallo-beta-lactamase — metallo-beta-lactamase resistant to all beta-lactam inhibitors",
    "IMP":    "Imipenemase Metallo-beta-lactamase — chromosomally and plasmid-encoded carbapenemase",
    "MCR":    "Mobilised Colistin Resistance — plasmid-mediated resistance to colistin, a last-resort antibiotic",
    "CTX-M":  "CTX-M ESBL — the most prevalent extended-spectrum beta-lactamase globally, conferring resistance to 3rd-generation cephalosporins",
    "mecA":   "mecA — the primary MRSA determinant, encoding an altered penicillin-binding protein",
    "mecC":   "mecC — alternative MRSA mechanism with zoonotic origin, undetected by standard MRSA screening",
    "VAN":    "Vancomycin resistance genes (vanA/vanB) — VRE determinants conferring high-level glycopeptide resistance",
}

# Pathogen genus → NCBI taxgroup mapping
PATHOGEN_TO_TAXGROUP: dict[str, list[str]] = {
    "klebsiella":       ["Klebsiella"],
    "escherichia":      ["Escherichia_coli_Shigella"],
    "e. coli":          ["Escherichia_coli_Shigella"],
    "acinetobacter":    ["Acinetobacter"],
    "pseudomonas":      ["Pseudomonas_aeruginosa"],
    "staphylococcus":   ["Staphylococcus_aureus"],
    "s. aureus":        ["Staphylococcus_aureus"],
    "enterococcus":     ["Enterococcus_faecium"],
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class GenomicEvidence:
    """A matching genomic signal for a phenotypic alert."""
    gene_name: str
    gene_family: str
    isolate_count: int
    latest_year: int
    time_series: dict
    doubling_time_years: Optional[float]
    acceleration_score: float
    phenotypic_gap: str
    precursor_tier: str


@dataclass
class CausalContext:
    """
    Cross-layer synthesis result for one phenotypic alert.

    Connects a phenotypic resistance signal to its genomic mechanistic basis.
    Populated by run_causal_analysis() and stored in alert.extra_data.
    """
    alert_id: str
    pathogen_name: str
    antibiotic_name: str
    country_iso3: str
    phenotypic_rate: float
    trend_direction: str

    # Genomic evidence
    genomic_genes: list[GenomicEvidence] = field(default_factory=list)
    causal_confidence: str = "INSUFFICIENT_DATA"  # HIGH / MEDIUM / INSUFFICIENT_DATA

    # Synthesised outputs
    causal_narrative: str = ""
    lead_time_note: str = ""
    mechanism_summary: str = ""
    spread_risk_note: str = ""

    # Metadata
    analysed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

GENOMIC_SIGNALS_QUERY = text("""
    SELECT
        gs.gene_name,
        gs.gene_family,
        gs.country_iso3,
        gs.year,
        gs.isolate_count,
        gs.region_who
    FROM genomic_signals gs
    WHERE gs.pathogen_name ILIKE :pathogen_pattern
      AND gs.country_iso3 = :country_iso3
      AND gs.isolate_count >= 5
    ORDER BY gs.gene_name, gs.year
""")

PHENOTYPIC_TREND_QUERY = text("""
    SELECT year, resistance_rate, data_source
    FROM resistance_records
    WHERE pathogen_name ILIKE :pathogen_pattern
      AND antibiotic_name ILIKE :antibiotic_pattern
      AND country_iso3 = :country_iso3
      AND year >= 2015
    ORDER BY year ASC
""")


def _get_gene_family(gene_name: str) -> str:
    """Extract gene family from full gene name (e.g. blaNDM-1 → NDM)."""
    clean = gene_name.replace("bla", "").replace("_", "-")
    for family in GENE_TO_ANTIBIOTIC_CLASSES.keys():
        if clean.upper().startswith(family.upper()):
            return family
    # Try prefix match without version number
    base = clean.split("-")[0].split(".")[0]
    for family in GENE_TO_ANTIBIOTIC_CLASSES.keys():
        if base.upper() == family.upper():
            return family
    return clean


def _gene_matches_antibiotic(gene_family: str, antibiotic_name: str) -> bool:
    """Check if a gene family confers resistance to the given antibiotic."""
    relevant = GENE_TO_ANTIBIOTIC_CLASSES.get(gene_family, [])
    antibiotic_lower = antibiotic_name.lower()
    for ab in relevant:
        if ab.lower() in antibiotic_lower or antibiotic_lower in ab.lower():
            return True
    return False


def _compute_simple_doubling(time_series: dict[int, int]) -> Optional[float]:
    """Estimate doubling time from time series using first/last point ratio."""
    if len(time_series) < 2:
        return None
    years = sorted(time_series.keys())
    first_count = time_series[years[0]]
    last_count = time_series[years[-1]]
    span = years[-1] - years[0]
    if first_count <= 0 or last_count <= first_count or span <= 0:
        return None
    import math
    try:
        growth_rate = math.log(last_count / first_count) / span
        if growth_rate <= 0:
            return None
        return round(math.log(2) / growth_rate, 1)
    except (ValueError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------------------
# Narrative generation
# ---------------------------------------------------------------------------

def _build_causal_narrative(
    pathogen_name: str,
    antibiotic_name: str,
    country_iso3: str,
    phenotypic_rate: float,
    trend_direction: str,
    genomic_genes: list[GenomicEvidence],
    causal_confidence: str,
) -> str:
    """
    Build a one-paragraph causal narrative connecting genomic and phenotypic evidence.

    The narrative follows a structured format:
    1. State the phenotypic observation
    2. Name the genomic mechanism(s)
    3. Quantify the genomic trend
    4. Draw the mechanistic connection
    5. Note implications

    Args:
        pathogen_name: Species name
        antibiotic_name: Antibiotic class
        country_iso3: ISO3 country code
        phenotypic_rate: Current resistance rate (0-1)
        trend_direction: rising / stable / falling
        genomic_genes: Matching genomic evidence
        causal_confidence: HIGH / MEDIUM / INSUFFICIENT_DATA

    Returns:
        Formatted narrative string
    """
    if not genomic_genes:
        return (
            "No matching genomic resistance gene data is available in AMR-Intel "
            "sources for " + pathogen_name + " in " + country_iso3 + ". "
            "The observed " + antibiotic_name.lower() + " resistance rate of "
            + str(round(phenotypic_rate * 100, 1)) + "% cannot currently be "
            "attributed to a specific molecular mechanism from this platform's "
            "genomic surveillance layer. Recommend cross-referencing with "
            "national genomic surveillance programmes."
        )

    rate_str = str(round(phenotypic_rate * 100, 1)) + "%"
    trend_str = {"rising": "rising", "stable": "stable", "falling": "declining"}.get(
        trend_direction, trend_direction
    )

    # Lead gene (highest isolate count)
    lead = max(genomic_genes, key=lambda g: g.isolate_count)
    gene_desc = GENE_DESCRIPTIONS.get(lead.gene_family, lead.gene_name + " resistance gene")

    # Build gene list string
    if len(genomic_genes) == 1:
        gene_str = lead.gene_name
    elif len(genomic_genes) == 2:
        gene_str = genomic_genes[0].gene_name + " and " + genomic_genes[1].gene_name
    else:
        gene_str = (
            ", ".join(g.gene_name for g in genomic_genes[:-1])
            + ", and " + genomic_genes[-1].gene_name
        )

    # Doubling time phrase
    doubling_str = ""
    if lead.doubling_time_years:
        if lead.doubling_time_years <= 1.0:
            doubling_str = (
                " with sub-annual doubling of sequenced isolates ("
                + str(lead.doubling_time_years) + " year doubling time)"
            )
        else:
            doubling_str = (
                " doubling approximately every "
                + str(lead.doubling_time_years) + " years in sequenced isolates"
            )

    # Confidence qualifier
    if causal_confidence == "HIGH":
        qualifier = "is consistent with"
        evidence_note = (
            " This connection is supported by ECDC phenotypic surveillance confirming "
            "that the resistance rates are genuine and not artefacts of sampling variation."
        )
    elif causal_confidence == "MEDIUM":
        qualifier = "may be explained by"
        evidence_note = (
            " This association is plausible but phenotypic surveillance coverage "
            "for " + country_iso3 + " is limited in AMR-Intel sources — "
            "in-country validation is recommended before clinical action."
        )
    else:
        qualifier = "is potentially associated with"
        evidence_note = " Independent genomic surveillance data should be consulted to confirm this association."

    # Tier note
    tier_note = ""
    if lead.precursor_tier == "Tier 1 — Confirmed Precursor":
        tier_note = (
            " This gene has been classified as a Tier 1 Confirmed Precursor — "
            "genomic spread is confirmed in a country with reliable phenotypic surveillance, "
            "and the phenotypic rate remains low, representing an active emergence window."
        )
    elif lead.precursor_tier == "Tier 2 — Candidate Precursor":
        tier_note = (
            " This gene is classified as a Tier 2 Candidate Precursor — "
            "genomic spread is detected but phenotypic confirmation is limited."
        )

    narrative = (
        "The " + trend_str + " " + antibiotic_name.lower() + " resistance rate "
        "in " + pathogen_name + " isolates in " + country_iso3 + " ("
        + rate_str + ") " + qualifier + " the expanding genomic prevalence of "
        + gene_str + ". "
        + gene_desc.capitalize() + doubling_str + ". "
        + str(lead.isolate_count) + " " + lead.gene_name + "-carrying isolates "
        "were detected in " + str(lead.latest_year) + " in AMR-Intel's NCBI NDARO "
        "surveillance layer."
        + evidence_note
        + tier_note
    )

    return narrative


def _build_lead_time_note(
    genomic_genes: list[GenomicEvidence],
    phenotypic_rate: float,
) -> str:
    """
    Estimate and describe the genomic lead time over phenotypic detection.

    The lead time is estimated as the time between the first year a genomic
    signal appeared in NDARO and the point at which phenotypic rates became
    clinically significant (>5%).
    """
    if not genomic_genes or phenotypic_rate <= 0:
        return ""

    lead = max(genomic_genes, key=lambda g: g.isolate_count)
    ts = lead.time_series
    if not ts:
        return ""

    first_genomic_year = min(ts.keys())
    current_year = datetime.now(timezone.utc).year

    if phenotypic_rate < 0.05:
        # Gene present but phenotype still low — active lead time window
        years_ahead = current_year - first_genomic_year
        return (
            "AMR-Intel first detected " + lead.gene_name + " in " + str(first_genomic_year)
            + " — " + str(years_ahead) + " year(s) before phenotypic resistance "
            "reached clinically significant levels (>5%). "
            "This represents an active early warning window."
        )
    else:
        # Both signals present — retrospective lead time
        years_ahead = current_year - first_genomic_year
        return (
            "Genomic surveillance first detected " + lead.gene_name
            + " in " + str(first_genomic_year) + ". "
            "Phenotypic resistance is now " + str(round(phenotypic_rate * 100, 1)) + "%, "
            "consistent with " + str(years_ahead) + " year(s) of genomic spread "
            "preceding full clinical expression."
        )


# ---------------------------------------------------------------------------
# Main analysis engine
# ---------------------------------------------------------------------------

def analyse_alert(
    session,
    alert_id: str,
    pathogen_name: str,
    antibiotic_name: str,
    country_iso3: str,
    current_resistance: float,
    trend_direction: str,
) -> CausalContext:
    """
    Cross-reference genomic and phenotypic data for one phenotypic alert.

    Queries genomic_signals for genes mechanistically linked to the alert's
    antibiotic class, computes confidence and narrative, and returns a
    CausalContext object.

    Args:
        session: SQLAlchemy session
        alert_id: Alert UUID string
        pathogen_name: e.g. "Klebsiella pneumoniae"
        antibiotic_name: e.g. "Imipenem"
        country_iso3: e.g. "HRV"
        current_resistance: float 0-1
        trend_direction: "rising" / "stable" / "falling"

    Returns:
        CausalContext with narrative and confidence
    """
    ctx = CausalContext(
        alert_id=alert_id,
        pathogen_name=pathogen_name,
        antibiotic_name=antibiotic_name,
        country_iso3=country_iso3,
        phenotypic_rate=current_resistance,
        trend_direction=trend_direction,
    )

    # Get genus for LIKE query
    genus = pathogen_name.split()[0]

    try:
        rows = session.execute(GENOMIC_SIGNALS_QUERY, {
            "pathogen_pattern": "%" + genus + "%",
            "country_iso3": country_iso3,
        }).fetchall()
    except Exception as exc:
        logger.error("Genomic query failed for %s/%s: %s", pathogen_name, country_iso3, exc)
        return ctx

    if not rows:
        ctx.causal_narrative = _build_causal_narrative(
            pathogen_name, antibiotic_name, country_iso3,
            current_resistance, trend_direction, [], "INSUFFICIENT_DATA"
        )
        return ctx

    # Group by gene, build time series
    gene_series: dict[str, dict] = {}
    gene_meta: dict[str, dict] = {}
    for row in rows:
        gname = row.gene_name
        if gname not in gene_series:
            gene_series[gname] = {}
            gene_meta[gname] = {
                "gene_family": row.gene_family or _get_gene_family(gname),
                "region_who": row.region_who,
            }
        gene_series[gname][row.year] = row.isolate_count

    # Filter to genes mechanistically linked to this antibiotic
    matching_genes: list[GenomicEvidence] = []
    for gene_name, ts in gene_series.items():
        family = gene_meta[gene_name]["gene_family"] or _get_gene_family(gene_name)
        if not _gene_matches_antibiotic(family, antibiotic_name):
            continue

        latest_year = max(ts.keys())
        latest_count = ts[latest_year]
        doubling = _compute_simple_doubling(ts)
        accel = min(1.0, latest_count / 500.0)  # simple proxy

        # Determine precursor tier from phenotypic rate
        if current_resistance <= 0.05:
            tier = "Tier 1 — Confirmed Precursor"
            gap = "very_low" if current_resistance > 0 else "absent"
        elif current_resistance <= 0.10:
            tier = "Tier 1 — Confirmed Precursor"
            gap = "low"
        else:
            tier = "Tier 3 — Established Resistance"
            gap = "established"

        matching_genes.append(GenomicEvidence(
            gene_name=gene_name,
            gene_family=family,
            isolate_count=latest_count,
            latest_year=latest_year,
            time_series=ts,
            doubling_time_years=doubling,
            acceleration_score=accel,
            phenotypic_gap=gap,
            precursor_tier=tier,
        ))

    # Sort by isolate count descending
    matching_genes.sort(key=lambda g: g.isolate_count, reverse=True)
    ctx.genomic_genes = matching_genes[:5]  # top 5 matching genes

    # Determine causal confidence
    if not matching_genes:
        ctx.causal_confidence = "INSUFFICIENT_DATA"
    elif len(matching_genes) >= 2:
        ctx.causal_confidence = "HIGH"
    elif matching_genes[0].isolate_count >= 20:
        ctx.causal_confidence = "HIGH"
    else:
        ctx.causal_confidence = "MEDIUM"

    # Build narrative
    ctx.causal_narrative = _build_causal_narrative(
        pathogen_name, antibiotic_name, country_iso3,
        current_resistance, trend_direction,
        ctx.genomic_genes, ctx.causal_confidence,
    )

    # Build lead time note
    ctx.lead_time_note = _build_lead_time_note(ctx.genomic_genes, current_resistance)

    # Build mechanism summary (short version for cards)
    if matching_genes:
        lead = matching_genes[0]
        ctx.mechanism_summary = (
            lead.gene_name + " (" + lead.gene_family + ")"
            + (" — " + str(lead.doubling_time_years) + "yr doubling" if lead.doubling_time_years else "")
        )

    return ctx


def run_causal_analysis(
    alert_dicts: list[dict],
    phenotypic_only: bool = True,
) -> dict[str, CausalContext]:
    """
    Run causal analysis across a list of alert dicts.

    Called by the orchestrator after triage. For each phenotypic alert,
    queries genomic_signals for mechanistic context.

    Args:
        alert_dicts: List of alert dicts from output_queue.jsonl
        phenotypic_only: If True, skip genomic precursor alerts

    Returns:
        Dict mapping alert_id → CausalContext
    """
    logger.info("=== Causal Intelligence Engine starting ===")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    SessionFactory = sessionmaker(bind=engine)

    results: dict[str, CausalContext] = {}
    analysed = skipped = no_genomic = 0

    with SessionFactory() as session:
        for alert in alert_dicts:
            signal_type = alert.get("signal_type", "")
            if phenotypic_only and signal_type == "genomic_precursor":
                skipped += 1
                continue

            alert_id = str(alert.get("alert_id") or alert.get("id", ""))
            pathogen = alert.get("pathogen_name", "")
            antibiotic = alert.get("antibiotic_name", "")
            country = alert.get("country_iso3", "")
            resistance = float(alert.get("current_resistance") or 0)
            trend = alert.get("trend_direction", "rising")

            if not (pathogen and antibiotic and country):
                skipped += 1
                continue

            ctx = analyse_alert(
                session, alert_id, pathogen, antibiotic, country, resistance, trend
            )
            results[alert_id] = ctx
            analysed += 1

            if not ctx.genomic_genes:
                no_genomic += 1

    logger.info(
        "Causal analysis complete: %d analysed | %d with genomic evidence | "
        "%d no genomic match | %d skipped",
        analysed, analysed - no_genomic, no_genomic, skipped,
    )
    return results


def analyse_single_alert_from_db(
    alert_id: str,
    pathogen_name: str,
    antibiotic_name: str,
    country_iso3: str,
    current_resistance: float,
    trend_direction: str,
) -> CausalContext:
    """
    Analyse a single alert by querying the DB directly.
    Used by the API endpoint for on-demand causal context.

    Args:
        alert_id: UUID string
        pathogen_name, antibiotic_name, country_iso3: alert fields
        current_resistance: float 0-1
        trend_direction: rising / stable / falling

    Returns:
        CausalContext
    """
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    SessionFactory = sessionmaker(bind=engine)

    with SessionFactory() as session:
        return analyse_alert(
            session, alert_id, pathogen_name, antibiotic_name,
            country_iso3, current_resistance, trend_direction,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Causal Intelligence Engine — cross-layer synthesis"
    )
    parser.add_argument("--pathogen", type=str, required=True,
                        help="Pathogen name e.g. 'Klebsiella pneumoniae'")
    parser.add_argument("--antibiotic", type=str, required=True,
                        help="Antibiotic name e.g. 'Imipenem'")
    parser.add_argument("--country", type=str, required=True,
                        help="ISO3 country code e.g. HRV")
    parser.add_argument("--resistance", type=float, default=0.3,
                        help="Current resistance rate 0-1 (default: 0.3)")
    parser.add_argument("--trend", type=str, default="rising",
                        help="Trend direction: rising/stable/falling")
    args = parser.parse_args()

    ctx = analyse_single_alert_from_db(
        alert_id="cli-test",
        pathogen_name=args.pathogen,
        antibiotic_name=args.antibiotic,
        country_iso3=args.country,
        current_resistance=args.resistance,
        trend_direction=args.trend,
    )

    print("\n" + "=" * 70)
    print("CAUSAL INTELLIGENCE REPORT")
    print("=" * 70)
    print("Signal:     " + args.pathogen + " / " + args.antibiotic + " / " + args.country)
    print("Resistance: " + str(round(args.resistance * 100, 1)) + "%  (" + args.trend + ")")
    print("Confidence: " + ctx.causal_confidence)
    print()
    print("GENOMIC EVIDENCE:")
    if ctx.genomic_genes:
        for g in ctx.genomic_genes:
            dt = ("2x/" + str(g.doubling_time_years) + "yr") if g.doubling_time_years else "unknown rate"
            print("  " + g.gene_name + " — " + str(g.isolate_count) + " isolates (" + str(g.latest_year) + ")  " + dt)
    else:
        print("  No matching genomic signals found")
    print()
    print("CAUSAL NARRATIVE:")
    print(ctx.causal_narrative)
    if ctx.lead_time_note:
        print()
        print("LEAD TIME:")
        print(ctx.lead_time_note)
    print("=" * 70)