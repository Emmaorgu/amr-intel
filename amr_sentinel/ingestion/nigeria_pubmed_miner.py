"""
Nigeria PubMed Miner
====================
Queries the NCBI PubMed/Entrez API for Nigerian AMR studies, extracts structured
resistance rate data from abstracts using Claude (claude-sonnet-4-6), and writes
normalised records to the resistance_records table with data_source='NIGERIA_LITERATURE'.

Inputs:
    - NCBI Entrez API (no key required for low-volume; NCBI_EMAIL env var recommended)
    - Anthropic API (ANTHROPIC_API_KEY env var required)
    - PostgreSQL amr_sentinel database (DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME env vars)

Outputs:
    - Rows in resistance_records table, country_iso3='NGA', data_source='NIGERIA_LITERATURE'
    - JSON extraction log at data/nigeria/pubmed_extraction_log.json

Dependencies:
    pip install biopython anthropic psycopg2-binary python-dotenv sqlalchemy
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import ssl
import urllib.request

import anthropic
from Bio import Entrez
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# SSL fix — bypasses self-signed / intercepting proxy certificates
# Common on Windows in corporate/ISP environments (Nigeria MTN, Airtel, etc.)
# ---------------------------------------------------------------------------

def _patch_ssl() -> None:
    """
    Install an unverified SSL context globally so Entrez HTTP calls succeed
    behind intercepting proxies that present self-signed certificates.
    Only applied when NCBI_SSL_VERIFY=false in environment.
    Safe for internal tooling; do not use in production web services.
    """
    skip_verify = os.getenv("NCBI_SSL_VERIFY", "false").lower() == "false"
    if skip_verify:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        https_handler = urllib.request.HTTPSHandler(context=ssl_ctx)
        opener = urllib.request.build_opener(https_handler)
        urllib.request.install_opener(opener)
        logging.getLogger("nigeria_pubmed_miner").warning(
            "SSL certificate verification DISABLED (NCBI_SSL_VERIFY=false). "
            "Traffic is still encrypted but certificate chain is not validated."
        )

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()
_patch_ssl()  # must run before any Entrez calls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("nigeria_pubmed_miner")

# NCBI Entrez requires an email for polite access
Entrez.email = os.getenv("NCBI_EMAIL", "amr-sentinel@example.com")
Entrez.tool = "amr-sentinel"

DATABASE_URL: str = (
    f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)





OUTPUT_DIR = Path("data/nigeria")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
EXTRACTION_LOG = OUTPUT_DIR / "pubmed_extraction_log.json"

# ---------------------------------------------------------------------------
# PubMed search queries — targeted to extract quantitative resistance data
# ---------------------------------------------------------------------------

SEARCH_QUERIES = [
    # Core clinical pathogens
    'Nigeria[MeSH Terms] AND "antimicrobial resistance"[Title/Abstract] AND "resistance rate"[Title/Abstract]',
    'Nigeria[MeSH Terms] AND "Escherichia coli"[Title/Abstract] AND "antibiotic resistance"[Title/Abstract] AND ("2018"[PDAT]:"2024"[PDAT])',
    'Nigeria[MeSH Terms] AND "Klebsiella pneumoniae"[Title/Abstract] AND "resistance"[Title/Abstract] AND ("2018"[PDAT]:"2024"[PDAT])',
    'Nigeria[MeSH Terms] AND "Staphylococcus aureus"[Title/Abstract] AND "methicillin"[Title/Abstract] AND ("2018"[PDAT]:"2024"[PDAT])',
    'Nigeria[MeSH Terms] AND "Pseudomonas aeruginosa"[Title/Abstract] AND "resistance"[Title/Abstract] AND ("2018"[PDAT]:"2024"[PDAT])',
    'Nigeria[MeSH Terms] AND "Acinetobacter baumannii"[Title/Abstract] AND "resistance"[Title/Abstract] AND ("2018"[PDAT]:"2024"[PDAT])',
    # Carbapenem / critical resistance
    'Nigeria[MeSH Terms] AND "carbapenem"[Title/Abstract] AND "resistance"[Title/Abstract]',
    'Nigeria[MeSH Terms] AND "ESBL"[Title/Abstract] AND ("2018"[PDAT]:"2024"[PDAT])',
    'Nigeria[MeSH Terms] AND "multidrug resistant"[Title/Abstract] AND "hospital"[Title/Abstract] AND ("2018"[PDAT]:"2024"[PDAT])',
    # LUTH / key institutions
    '"Lagos University Teaching Hospital"[Title/Abstract] AND "resistance"[Title/Abstract]',
    '"LUTH"[Title/Abstract] AND "antimicrobial"[Title/Abstract]',
    'Nigeria[MeSH Terms] AND "ciprofloxacin resistance"[Title/Abstract]',
    'Nigeria[MeSH Terms] AND "ceftriaxone resistance"[Title/Abstract]',
]

MAX_RESULTS_PER_QUERY = 20  # cap per query to stay within polite Entrez limits
ENTREZ_DELAY_SECONDS = 0.4  # NCBI rate limit: 3 req/sec without API key


# ---------------------------------------------------------------------------
# Pathogen and antibiotic normalisation maps
# ---------------------------------------------------------------------------

PATHOGEN_MAP: dict[str, str] = {
    "escherichia coli": "Escherichia coli",
    "e. coli": "Escherichia coli",
    "e.coli": "Escherichia coli",
    "klebsiella pneumoniae": "Klebsiella pneumoniae",
    "k. pneumoniae": "Klebsiella pneumoniae",
    "k.pneumoniae": "Klebsiella pneumoniae",
    "staphylococcus aureus": "Staphylococcus aureus",
    "s. aureus": "Staphylococcus aureus",
    "mrsa": "Staphylococcus aureus",
    "methicillin-resistant staphylococcus aureus": "Staphylococcus aureus",
    "pseudomonas aeruginosa": "Pseudomonas aeruginosa",
    "p. aeruginosa": "Pseudomonas aeruginosa",
    "acinetobacter baumannii": "Acinetobacter baumannii",
    "acinetobacter": "Acinetobacter baumannii",
    "enterococcus faecium": "Enterococcus faecium",
    "e. faecium": "Enterococcus faecium",
    "enterococcus faecalis": "Enterococcus faecalis",
    "e. faecalis": "Enterococcus faecalis",
    "streptococcus pneumoniae": "Streptococcus pneumoniae",
    "s. pneumoniae": "Streptococcus pneumoniae",
}

ANTIBIOTIC_CLASS_MAP: dict[str, str] = {
    "ciprofloxacin": "Fluoroquinolone",
    "ofloxacin": "Fluoroquinolone",
    "levofloxacin": "Fluoroquinolone",
    "ceftriaxone": "Third-generation cephalosporin",
    "ceftazidime": "Third-generation cephalosporin",
    "cefotaxime": "Third-generation cephalosporin",
    "cefuroxime": "Second-generation cephalosporin",
    "ampicillin": "Penicillin",
    "amoxicillin": "Penicillin",
    "amoxicillin/clavulanic acid": "Beta-lactam/beta-lactamase inhibitor",
    "augmentin": "Beta-lactam/beta-lactamase inhibitor",
    "piperacillin/tazobactam": "Beta-lactam/beta-lactamase inhibitor",
    "imipenem": "Carbapenem",
    "meropenem": "Carbapenem",
    "ertapenem": "Carbapenem",
    "gentamicin": "Aminoglycoside",
    "amikacin": "Aminoglycoside",
    "tetracycline": "Tetracycline",
    "doxycycline": "Tetracycline",
    "trimethoprim/sulfamethoxazole": "Folate synthesis inhibitor",
    "cotrimoxazole": "Folate synthesis inhibitor",
    "trimethoprim-sulfamethoxazole": "Folate synthesis inhibitor",
    "chloramphenicol": "Amphenicol",
    "nitrofurantoin": "Nitrofuran",
    "vancomycin": "Glycopeptide",
    "methicillin": "Penicillinase-resistant penicillin",
    "oxacillin": "Penicillinase-resistant penicillin",
    "colistin": "Polymyxin",
    "polymyxin b": "Polymyxin",
}


def normalise_pathogen(raw: str) -> Optional[str]:
    """Return canonical pathogen name or None if unrecognised."""
    key = raw.strip().lower()
    return PATHOGEN_MAP.get(key)


def normalise_antibiotic(raw: str) -> tuple[str, str]:
    """Return (canonical_name, antibiotic_class) for a raw antibiotic string."""
    key = raw.strip().lower()
    canonical = raw.strip().title()  # fallback: title-case the raw string
    ab_class = ANTIBIOTIC_CLASS_MAP.get(key, "Other")
    # Try partial match for class lookup
    for known_ab, known_class in ANTIBIOTIC_CLASS_MAP.items():
        if known_ab in key:
            canonical = known_ab.title()
            ab_class = known_class
            break
    return canonical, ab_class


# ---------------------------------------------------------------------------
# Entrez helpers
# ---------------------------------------------------------------------------


def search_pubmed(query: str, max_results: int = MAX_RESULTS_PER_QUERY) -> list[str]:
    """
    Run an Entrez esearch query and return a list of PubMed IDs.

    Args:
        query: PubMed search string
        max_results: Maximum number of PMIDs to return

    Returns:
        List of PMID strings

    Raises:
        RuntimeError: If Entrez search fails
    """
    try:
        time.sleep(ENTREZ_DELAY_SECONDS)
        handle = Entrez.esearch(db="pubmed", term=query, retmax=max_results, sort="relevance")
        record = Entrez.read(handle)
        handle.close()
        pmids = record.get("IdList", [])
        logger.info("Query returned %d PMIDs: %s", len(pmids), query[:80])
        return pmids
    except Exception as exc:
        raise RuntimeError(f"Entrez esearch failed for query '{query[:60]}': {exc}") from exc


def fetch_abstracts(pmids: list[str]) -> list[dict]:
    """
    Fetch title + abstract for a list of PMIDs via Entrez efetch.

    Args:
        pmids: List of PubMed ID strings

    Returns:
        List of dicts with keys: pmid, title, abstract, year, journal
    """
    if not pmids:
        return []

    try:
        time.sleep(ENTREZ_DELAY_SECONDS)
        handle = Entrez.efetch(
            db="pubmed",
            id=",".join(pmids),
            rettype="xml",
            retmode="xml",
        )
        records = Entrez.read(handle)
        handle.close()
    except Exception as exc:
        logger.error("Entrez efetch failed for %d PMIDs: %s", len(pmids), exc)
        return []

    articles = []
    for article in records.get("PubmedArticle", []):
        try:
            medline = article["MedlineCitation"]
            pmid = str(medline["PMID"])
            art_data = medline["Article"]

            title = str(art_data.get("ArticleTitle", ""))

            # Abstract may be structured (list of sections) or plain string
            abstract_raw = art_data.get("Abstract", {}).get("AbstractText", "")
            if isinstance(abstract_raw, list):
                abstract = " ".join(str(s) for s in abstract_raw)
            else:
                abstract = str(abstract_raw)

            # Year
            pub_date = art_data.get("Journal", {}).get("JournalIssue", {}).get("PubDate", {})
            year_raw = pub_date.get("Year") or pub_date.get("MedlineDate", "")[:4]
            try:
                year = int(year_raw)
            except (ValueError, TypeError):
                year = datetime.now().year

            journal = str(art_data.get("Journal", {}).get("Title", ""))

            if abstract and len(abstract) > 100:
                articles.append(
                    {
                        "pmid": pmid,
                        "title": title,
                        "abstract": abstract,
                        "year": year,
                        "journal": journal,
                    }
                )
        except (KeyError, TypeError) as exc:
            logger.debug("Skipping malformed article record: %s", exc)

    logger.info("Fetched %d articles with usable abstracts from %d PMIDs", len(articles), len(pmids))
    return articles


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """You are a biomedical data extraction specialist. 
Your task is to extract structured antimicrobial resistance data from scientific abstracts.

Extract ONLY data explicitly stated with a numeric resistance percentage or rate.
Do NOT infer or estimate — only extract values directly present in the text.

Return a JSON array. Each element must have EXACTLY these fields:
{
  "pathogen": "exact organism name as written in the abstract",
  "antibiotic": "exact antibiotic name as written",
  "resistance_rate": 0.75,  // decimal 0.0-1.0 (convert from percentage e.g. 75% → 0.75)
  "sample_count": 200,       // integer number of isolates tested, or null if not stated
  "year": 2022,              // study year or collection year if stated, else publication year
  "hospital_or_location": "LUTH Lagos" // specific hospital or Nigerian city/state if mentioned, else "Nigeria"
}

Rules:
- resistance_rate must be a float between 0.0 and 1.0
- Only include entries where resistance_rate is explicitly numeric in the abstract
- If the same pathogen/antibiotic pair appears multiple times, include each separately
- Do not include susceptibility rates (only resistance)
- Return [] if no quantitative resistance data is present
- Return ONLY the JSON array, no preamble, no markdown fences"""



# ---------------------------------------------------------------------------
# Biological plausibility bounds
# Based on WHO priority pathogen data and global surveillance literature.
# Rates exceeding these thresholds are almost certainly extraction errors
# (e.g. "100% of isolates were tested" misread as "100% were resistant").
# ---------------------------------------------------------------------------

# (pathogen_substring, antibiotic_substring) -> max_plausible_rate
PLAUSIBILITY_BOUNDS: dict[tuple[str, str], float] = {
    # Vancomycin resistance in S. aureus: globally <3%, any higher is VRSA
    # which is exceedingly rare (fewer than 20 confirmed cases ever in Africa)
    ("staphylococcus aureus", "vancomycin"): 0.05,
    # Linezolid resistance in S. aureus: globally <1%
    ("staphylococcus aureus", "linezolid"): 0.03,
    # Carbapenem resistance in E. coli: globally <5% except hotspots
    ("escherichia coli", "meropenem"): 0.25,
    ("escherichia coli", "imipenem"): 0.25,
    ("escherichia coli", "ertapenem"): 0.25,
    # Vancomycin resistance in S. pneumoniae: essentially zero
    ("streptococcus pneumoniae", "vancomycin"): 0.02,
    # Colistin susceptibility: resistance >30% is implausible in most contexts
    ("escherichia coli", "colistin"): 0.30,
    ("klebsiella pneumoniae", "colistin"): 0.30,
}

# Absolute ceiling: no resistance rate >97% is credible for population-level
# surveillance (100% rates in small studies = testing artifact)
ABSOLUTE_MAX_RATE = 0.97
MINIMUM_SAMPLE_SIZE_FOR_HIGH_RATE = 30  # rates >80% need n>=30 to be credible


def _check_plausibility(
    pathogen: str,
    antibiotic: str,
    rate: float,
    pmid: str,
    sample_count: Optional[int] = None,
) -> Optional[str]:
    """
    Check whether an extracted resistance rate is biologically plausible.

    Args:
        pathogen: Pathogen name as extracted
        antibiotic: Antibiotic name as extracted
        rate: Resistance rate (0.0-1.0)
        pmid: Source PMID for logging
        sample_count: Number of isolates tested (if available)

    Returns:
        Rejection reason string if implausible, None if plausible
    """
    pathogen_lower = pathogen.lower()
    antibiotic_lower = antibiotic.lower()

    # Check specific known bounds
    for (path_sub, ab_sub), max_rate in PLAUSIBILITY_BOUNDS.items():
        if path_sub in pathogen_lower and ab_sub in antibiotic_lower:
            if rate > max_rate:
                return (
                    f"{pathogen}/{antibiotic} rate {rate:.1%} exceeds biological "
                    f"plausibility bound {max_rate:.1%} — likely extraction error"
                )

    # Check absolute ceiling
    if rate >= ABSOLUTE_MAX_RATE:
        # Allow if large sample AND not a known implausible combo
        if sample_count and sample_count >= MINIMUM_SAMPLE_SIZE_FOR_HIGH_RATE:
            # Still reject 100% exactly — too likely to be a testing coverage stat
            if rate == 1.0:
                return (
                    f"{pathogen}/{antibiotic} rate = 100% (n={sample_count}) — "
                    f"100% resistance is almost always a testing coverage artefact, not true resistance"
                )
        elif not sample_count:
            if rate == 1.0:
                return (
                    f"{pathogen}/{antibiotic} rate = 100% with unknown sample size — "
                    f"rejecting as likely extraction error"
                )

    return None

def extract_resistance_data(
    client: anthropic.Anthropic,
    article: dict,
) -> list[dict]:
    """
    Use Claude to extract structured resistance data from a PubMed abstract.

    Args:
        client: Anthropic client instance
        article: Dict with pmid, title, abstract, year, journal

    Returns:
        List of extracted resistance dicts, or empty list if none found
    """
    prompt = (
        "Extract antimicrobial resistance data from this Nigerian study abstract.\n\n"
        "TITLE: " + article["title"] + "\n\n"
        "ABSTRACT: " + article["abstract"][:3000]  # cap to avoid token overflow
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()

        extracted = json.loads(raw)
        if not isinstance(extracted, list):
            logger.warning("Claude returned non-list for PMID %s", article["pmid"])
            return []

        # Validate each record
        valid = []
        for item in extracted:
            rate = item.get("resistance_rate")
            if not isinstance(rate, (int, float)):
                continue
            if not (0.0 <= float(rate) <= 1.0):
                continue
            if not item.get("pathogen") or not item.get("antibiotic"):
                continue
            # Biological plausibility check
            rejection = _check_plausibility(
                item["pathogen"], item["antibiotic"], float(rate),
                article["pmid"], item.get("sample_count")
            )
            if rejection:
                logger.warning("PMID %s - rejected implausible: %s", article["pmid"], rejection)
                continue
            valid.append(item)

        logger.info(
            "PMID %s — extracted %d valid resistance records",
            article["pmid"],
            len(valid),
        )
        return valid

    except json.JSONDecodeError as exc:
        logger.warning("JSON parse failed for PMID %s: %s", article["pmid"], exc)
        return []
    except anthropic.APIError as exc:
        logger.error("Claude API error for PMID %s: %s", article["pmid"], exc)
        return []


# ---------------------------------------------------------------------------
# Database writer
# ---------------------------------------------------------------------------


def get_db_engine():
    """Create and return SQLAlchemy engine."""
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def write_records_to_db(
    session_factory,
    records: list[dict],
    article: dict,
) -> int:
    """
    Write extracted resistance records to resistance_records table.
    Idempotent — skips records already present by (pathogen, antibiotic,
    country_iso3, year, data_source, source_record_id).

    Args:
        session_factory: SQLAlchemy sessionmaker
        records: List of extracted resistance dicts from Claude
        article: Source article metadata dict

    Returns:
        Number of rows inserted (not counting skipped duplicates)
    """
    inserted = 0

    insert_sql = text("""
        INSERT INTO resistance_records (
            pathogen_name,
            pathogen_ncbi_id,
            antibiotic_name,
            antibiotic_class,
            country_iso3,
            region_who,
            year,
            quarter,
            resistance_rate,
            sample_count,
            data_source,
            source_record_id,
            ingested_at
        )
        VALUES (
            :pathogen_name,
            :pathogen_ncbi_id,
            :antibiotic_name,
            :antibiotic_class,
            :country_iso3,
            :region_who,
            :year,
            :quarter,
            :resistance_rate,
            :sample_count,
            :data_source,
            :source_record_id,
            :ingested_at
        )
        ON CONFLICT (pathogen_name, antibiotic_name, country_iso3, year, quarter, data_source, source_record_id)
        DO NOTHING
    """)

    with session_factory() as session:
        for rec in records:
            pathogen_canonical = normalise_pathogen(rec["pathogen"]) or rec["pathogen"].strip()
            antibiotic_canonical, ab_class = normalise_antibiotic(rec["antibiotic"])

            # Build a unique source_record_id: pmid + pathogen slug + antibiotic slug
            pathogen_slug = re.sub(r"[^a-z0-9]", "_", rec["pathogen"].lower())[:30]
            antibiotic_slug = re.sub(r"[^a-z0-9]", "_", rec["antibiotic"].lower())[:20]
            source_record_id = "PMID_" + article["pmid"] + "_" + pathogen_slug + "_" + antibiotic_slug

            try:
                result = session.execute(
                    insert_sql,
                    {
                        "pathogen_name": pathogen_canonical,
                        "pathogen_ncbi_id": None,
                        "antibiotic_name": antibiotic_canonical,
                        "antibiotic_class": ab_class,
                        "country_iso3": "NGA",
                        "region_who": "AFRO",
                        "year": int(rec.get("year") or article["year"] or datetime.utcnow().year),
                        "quarter": None,
                        "resistance_rate": float(rec["resistance_rate"]),
                        "sample_count": int(rec["sample_count"]) if rec.get("sample_count") else None,
                        "data_source": "NIGERIA_LITERATURE",
                        "source_record_id": source_record_id,
                        "ingested_at": datetime.utcnow(),
                    },
                )
                if result.rowcount > 0:
                    inserted += 1
            except Exception as exc:
                logger.error("DB insert failed for PMID %s record: %s", article["pmid"], exc)
                session.rollback()
                continue

        session.commit()

    return inserted


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_nigeria_pubmed_miner(dry_run: bool = False) -> dict:
    """
    Full pipeline: search PubMed → fetch abstracts → extract with Claude → write to DB.

    Args:
        dry_run: If True, extract data but do not write to DB

    Returns:
        Summary dict: articles_found, articles_processed, records_extracted, records_inserted
    """
    logger.info("=== Nigeria PubMed Miner starting ===")
    if dry_run:
        logger.info("DRY RUN MODE — no database writes")

    anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    engine = get_db_engine() if not dry_run else None
    SessionFactory = sessionmaker(bind=engine) if engine else None

    # Deduplicate PMIDs across queries
    seen_pmids: set[str] = set()
    all_articles: list[dict] = []

    for query in SEARCH_QUERIES:
        try:
            pmids = search_pubmed(query)
            new_pmids = [p for p in pmids if p not in seen_pmids]
            seen_pmids.update(new_pmids)

            if new_pmids:
                articles = fetch_abstracts(new_pmids)
                all_articles.extend(articles)
        except RuntimeError as exc:
            logger.error("Search query failed, skipping: %s", exc)

    logger.info("Total unique articles with abstracts: %d", len(all_articles))

    extraction_log = []
    total_extracted = 0
    total_inserted = 0

    for article in all_articles:
        records = extract_resistance_data(anthropic_client, article)
        total_extracted += len(records)

        log_entry = {
            "pmid": article["pmid"],
            "title": article["title"][:120],
            "year": article["year"],
            "journal": article["journal"],
            "records_extracted": len(records),
            "records": records,
        }
        extraction_log.append(log_entry)

        if records and not dry_run and SessionFactory:
            inserted = write_records_to_db(SessionFactory, records, article)
            total_inserted += inserted
            log_entry["records_inserted"] = inserted
        else:
            log_entry["records_inserted"] = 0

        # Polite delay between Claude calls
        time.sleep(0.2)

    # Save extraction log
    with open(EXTRACTION_LOG, "w", encoding="utf-8") as f:
        json.dump(extraction_log, f, indent=2, ensure_ascii=False)
    logger.info("Extraction log saved to %s", EXTRACTION_LOG)

    summary = {
        "articles_found": len(all_articles),
        "articles_with_data": sum(1 for e in extraction_log if e["records_extracted"] > 0),
        "records_extracted": total_extracted,
        "records_inserted": total_inserted,
        "dry_run": dry_run,
        "run_at": datetime.utcnow().isoformat(),
    }

    logger.info("=== Nigeria PubMed Miner complete ===")
    logger.info(
        "Articles: %d found, %d with data | Records: %d extracted, %d inserted",
        summary["articles_found"],
        summary["articles_with_data"],
        summary["records_extracted"],
        summary["records_inserted"],
    )

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Mine Nigerian AMR resistance data from PubMed")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract data but do not write to database",
    )
    args = parser.parse_args()

    summary = run_nigeria_pubmed_miner(dry_run=args.dry_run)
    print("\n=== SUMMARY ===")
    for key, value in summary.items():
        print(f"  {key}: {value}")