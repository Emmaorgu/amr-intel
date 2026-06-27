"""
NCBI NDARO Genomic Ingestor
============================
Polls the NCBI Pathogen Detection FTP site for resistance gene submissions
(NDM-1, OXA-48, MCR-1, KPC, VIM, IMP, OXA-23, CTX-M-15) across all geographies,
extracts country-level gene detection counts by year, and writes normalised
records to a new `genomic_signals` table.

This is distinct from resistance_records (which stores phenotypic resistance rates).
Genomic signals represent pre-phenotypic early warning: a resistance gene detected
in a country's sequenced isolates before clinical phenotypic resistance appears
in surveillance data.

Signal type produced: 'genomic_precursor'
— gene detected in a geography with no or low phenotypic resistance signal yet.

Data source:
    NCBI Pathogen Detection FTP: ftp.ncbi.nlm.nih.gov/pathogen/Results/
    Updated approximately daily per taxgroup (bacterial species).
    No authentication required. Public data.

FTP structure:
    /pathogen/Results/{TaxGroup}/latest_snps/Metadata/
        {TaxGroup}.metadata.tsv      — isolate metadata (country, date, source)
        {TaxGroup}.amr.metadata.tsv  — AMR gene calls per isolate

Key fields in AMR metadata:
    - element_symbol: gene name (e.g. "blaNDM-1", "blaOXA-48", "mcr-1")
    - element_type: e.g. "AMR"
    - geo_loc_name: country/region of collection
    - isolation_source: human/environmental/food/animal
    - collection_date: YYYY or YYYY-MM or YYYY-MM-DD

Inputs:
    - NCBI Pathogen Detection FTP (public, no auth)
    - PostgreSQL amr_sentinel database (DB_* env vars)

Outputs:
    - Rows in `genomic_signals` table (new table — see schema below)
    - JSON run log at data/genomic/ndaro_run_log.json

New table schema (create via Alembic or pgAdmin before first run):
    CREATE TABLE genomic_signals (
        id                  SERIAL PRIMARY KEY,
        gene_name           TEXT NOT NULL,          -- e.g. "blaNDM-1"
        gene_family         TEXT NOT NULL,          -- e.g. "NDM"
        drug_class          TEXT NOT NULL,          -- e.g. "Carbapenem"
        pathogen_name       TEXT NOT NULL,          -- e.g. "Klebsiella pneumoniae"
        country_iso3        TEXT NOT NULL,
        region_who          TEXT,
        year                INTEGER NOT NULL,
        isolate_count       INTEGER NOT NULL,       -- number of isolates with this gene
        data_source         TEXT DEFAULT 'NCBI_NDARO',
        last_updated        TIMESTAMP,
        CONSTRAINT uq_genomic_signal UNIQUE (gene_name, pathogen_name, country_iso3, year)
    );

Dependencies:
    pip install biopython python-dotenv sqlalchemy psycopg2-binary requests
"""

import ftplib
import gzip
import io
import json
import logging
import os
import re
import ssl
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

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
logger = logging.getLogger("ndaro_ingestor")

DATABASE_URL: str = (
    f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)

OUTPUT_DIR = Path("data/genomic")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RUN_LOG = OUTPUT_DIR / "ndaro_run_log.json"

NCBI_FTP_HOST = "ftp.ncbi.nlm.nih.gov"
NCBI_FTP_BASE = "/pathogen/Results"

# ---------------------------------------------------------------------------
# SSL fix — same pattern as nigeria_pubmed_miner.py
# ---------------------------------------------------------------------------

def _patch_ssl() -> None:
    """Bypass SSL certificate verification for intercepting proxy environments."""
    skip_verify = os.getenv("NCBI_SSL_VERIFY", "false").lower() == "false"
    if skip_verify:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        https_handler = urllib.request.HTTPSHandler(context=ssl_ctx)
        opener = urllib.request.build_opener(https_handler)
        urllib.request.install_opener(opener)
        logger.warning(
            "SSL certificate verification DISABLED (NCBI_SSL_VERIFY=false). "
            "Traffic is still encrypted but certificate chain is not validated."
        )

_patch_ssl()

# ---------------------------------------------------------------------------
# Target pathogens and taxgroup names
# These correspond to NCBI Pathogen Detection taxgroup directory names
# ---------------------------------------------------------------------------

TAXGROUPS: list[dict] = [
    {
        "taxgroup": "Klebsiella",
        "pathogen_name": "Klebsiella pneumoniae",
        "who_priority": "CRITICAL",
    },
    {
        "taxgroup": "Escherichia_coli_Shigella",
        "pathogen_name": "Escherichia coli",
        "who_priority": "CRITICAL",
    },
    {
        "taxgroup": "Acinetobacter",
        "pathogen_name": "Acinetobacter baumannii",
        "who_priority": "CRITICAL",
    },
    {
        "taxgroup": "Pseudomonas_aeruginosa",
        "pathogen_name": "Pseudomonas aeruginosa",
        "who_priority": "CRITICAL",
    },
    {
        "taxgroup": "Staphylococcus_aureus",
        "pathogen_name": "Staphylococcus aureus",
        "who_priority": "HIGH",
    },
    {
        "taxgroup": "Enterococcus_faecium",
        "pathogen_name": "Enterococcus faecium",
        "who_priority": "HIGH",
    },
]

# ---------------------------------------------------------------------------
# Priority resistance genes — the signals we care most about
# ---------------------------------------------------------------------------

PRIORITY_GENES: dict[str, dict] = {
    # Carbapenemases — most critical
    "blaNDM": {"family": "NDM", "drug_class": "Carbapenem", "priority": 1},
    "blaOXA-48": {"family": "OXA-48", "drug_class": "Carbapenem", "priority": 1},
    "blaOXA-23": {"family": "OXA-23", "drug_class": "Carbapenem", "priority": 1},
    "blaKPC": {"family": "KPC", "drug_class": "Carbapenem", "priority": 1},
    "blaVIM": {"family": "VIM", "drug_class": "Carbapenem", "priority": 1},
    "blaIMP": {"family": "IMP", "drug_class": "Carbapenem", "priority": 1},
    # Colistin resistance — last resort
    "mcr": {"family": "MCR", "drug_class": "Colistin", "priority": 1},
    # ESBL — high importance
    "blaCTX-M": {"family": "CTX-M", "drug_class": "Third-generation cephalosporin", "priority": 2},
    # Methicillin resistance (MRSA)
    "mecA": {"family": "mecA", "drug_class": "Methicillin", "priority": 2},
    "mecC": {"family": "mecC", "drug_class": "Methicillin", "priority": 2},
    # Vancomycin resistance
    "vanA": {"family": "VAN", "drug_class": "Vancomycin", "priority": 2},
    "vanB": {"family": "VAN", "drug_class": "Vancomycin", "priority": 2},
}

# ---------------------------------------------------------------------------
# ISO3 country code mapping (subset — common NCBI country names)
# NCBI uses "Country" field which may include region qualifiers
# ---------------------------------------------------------------------------

COUNTRY_ISO3: dict[str, str] = {
    "Nigeria": "NGA", "Ghana": "GHA", "Kenya": "KEN", "South Africa": "ZAF",
    "Ethiopia": "ETH", "Tanzania": "TZA", "Uganda": "UGA", "Cameroon": "CMR",
    "Senegal": "SEN", "Mali": "MLI", "Ivory Coast": "CIV", "Cote d'Ivoire": "CIV",
    "USA": "USA", "United States": "USA", "United Kingdom": "GBR", "UK": "GBR",
    "Germany": "DEU", "France": "FRA", "Italy": "ITA", "Spain": "ESP",
    "China": "CHN", "India": "IND", "Pakistan": "PAK", "Bangladesh": "BGD",
    "Thailand": "THA", "Vietnam": "VNM", "Indonesia": "IDN", "Philippines": "PHL",
    "Brazil": "BRA", "Colombia": "COL", "Argentina": "ARG", "Mexico": "MEX",
    "Egypt": "EGY", "Tunisia": "TUN", "Morocco": "MAR", "Algeria": "DZA",
    "Saudi Arabia": "SAU", "Iran": "IRN", "Turkey": "TUR", "Israel": "ISR",
    "Bulgaria": "BGR", "Romania": "ROU", "Croatia": "HRV", "Greece": "GRC",
    "Poland": "POL", "Czech Republic": "CZE", "Slovakia": "SVK", "Cyprus": "CYP",
    "Australia": "AUS", "New Zealand": "NZL", "Japan": "JPN", "South Korea": "KOR",
    "Canada": "CAN", "Russia": "RUS", "Ukraine": "UKR", "Sweden": "SWE",
    "Netherlands": "NLD", "Belgium": "BEL", "Switzerland": "CHE", "Austria": "AUT",
    "Portugal": "PRT", "Denmark": "DNK", "Norway": "NOR", "Finland": "FIN",
    "Iceland": "ISL", "Ireland": "IRL", "Lithuania": "LTU", "Latvia": "LVA",
    "Estonia": "EST", "Hungary": "HUN", "Serbia": "SRB", "Croatia": "HRV",
    "Slovenia": "SVN", "Bosnia and Herzegovina": "BIH", "Albania": "ALB",
    "North Macedonia": "MKD", "Luxembourg": "LUX", "Malta": "MLT",
}

WHO_REGION: dict[str, str] = {
    "NGA": "AFRO", "GHA": "AFRO", "KEN": "AFRO", "ZAF": "AFRO", "ETH": "AFRO",
    "TZA": "AFRO", "UGA": "AFRO", "CMR": "AFRO", "SEN": "AFRO", "MLI": "AFRO",
    "CIV": "AFRO", "EGY": "EMRO", "TUN": "EMRO", "MAR": "EMRO", "DZA": "EMRO",
    "SAU": "EMRO", "IRN": "EMRO", "USA": "AMRO", "BRA": "AMRO", "COL": "AMRO",
    "ARG": "AMRO", "MEX": "AMRO", "CAN": "AMRO", "GBR": "EURO", "DEU": "EURO",
    "FRA": "EURO", "ITA": "EURO", "ESP": "EURO", "BGR": "EURO", "ROU": "EURO",
    "HRV": "EURO", "GRC": "EURO", "POL": "EURO", "CZE": "EURO", "SVK": "EURO",
    "CYP": "EURO", "TUR": "EURO", "ISL": "EURO", "IRL": "EURO", "LTU": "EURO",
    "CHN": "WPRO", "JPN": "WPRO", "KOR": "WPRO", "AUS": "WPRO", "NZL": "WPRO",
    "IND": "SEARO", "BGD": "SEARO", "THA": "SEARO", "IDN": "SEARO",
    "PAK": "EMRO", "ISR": "EURO", "RUS": "EURO", "UKR": "EURO",
}


def get_iso3(raw_country: str) -> Optional[str]:
    """
    Extract ISO3 country code from NCBI geo_loc_name field.
    NCBI format is often "Country:Region" e.g. "Nigeria:Lagos"

    Args:
        raw_country: Raw geo_loc_name string from NCBI metadata

    Returns:
        ISO3 code or None if country not in mapping
    """
    if not raw_country or raw_country in ("missing", "not collected", "not applicable", "N/A"):
        return None
    # Strip region qualifier
    country = raw_country.split(":")[0].strip()
    return COUNTRY_ISO3.get(country)


def match_priority_gene(element_symbol: str) -> Optional[tuple[str, dict]]:
    """
    Match an AMRFinderPlus element_symbol to our priority gene list.
    Returns (canonical_gene_prefix, gene_info) or None if not priority.

    Args:
        element_symbol: Gene symbol from AMRFinderPlus e.g. "blaNDM-1", "mcr-3.1"

    Returns:
        Tuple of (gene_prefix, gene_dict) or None
    """
    for prefix, info in PRIORITY_GENES.items():
        if element_symbol.startswith(prefix):
            return prefix, info
    return None


# ---------------------------------------------------------------------------
# FTP downloader
# ---------------------------------------------------------------------------

def _resolve_latest_version(ftp: ftplib.FTP, taxgroup: str) -> Optional[str]:
    """
    Resolve the latest_snps symlink to get the actual versioned directory name.
    FTP symlinks are listed as: "latest_snps -> PDG000000012.2449"

    Args:
        ftp: Connected FTP instance
        taxgroup: Taxgroup name

    Returns:
        Resolved version string e.g. "PDG000000012.2449", or None on failure
    """
    try:
        lines = []
        ftp.retrlines(f"LIST {NCBI_FTP_BASE}/{taxgroup}/", lines.append)
        for line in lines:
            # Match symlink entries: "... latest_snps -> PDG000000012.2449"
            if "latest_snps" in line and "->" in line:
                version = line.split("->")[-1].strip()
                logger.info("Resolved latest_snps -> %s for %s", version, taxgroup)
                return version
        logger.error("Could not find latest_snps symlink for %s", taxgroup)
        return None
    except ftplib.all_errors as exc:
        logger.error("Failed to resolve latest version for %s: %s", taxgroup, exc)
        return None


def download_amr_metadata(taxgroup: str) -> Optional[io.StringIO]:
    """
    Download and decompress the AMR metadata TSV for a taxgroup from NCBI FTP.

    Resolves the latest_snps symlink to the actual versioned directory, then
    downloads: /pathogen/Results/{taxgroup}/{version}/Metadata/{taxgroup}.amr.metadata.tsv.gz

    Args:
        taxgroup: NCBI taxgroup name e.g. "Klebsiella"

    Returns:
        StringIO of the decompressed TSV content, or None on failure
    """
    try:
        ftp = ftplib.FTP(NCBI_FTP_HOST, timeout=120)
        ftp.login()

        # Resolve symlink to get actual versioned directory
        version = _resolve_latest_version(ftp, taxgroup)
        if not version:
            ftp.quit()
            return None

        ftp_path = f"{NCBI_FTP_BASE}/{taxgroup}/{version}/AMR/{version}.amr.metadata.tsv"
        logger.info("Downloading: %s", ftp_path)

        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {ftp_path}", buf.write)
        ftp.quit()

        buf.seek(0)
        raw = buf.read().decode("utf-8", errors="replace")

        logger.info(
            "Downloaded %s — %.1f MB decompressed",
            taxgroup,
            len(raw) / 1024 / 1024,
        )
        return io.StringIO(raw)

    except ftplib.all_errors as exc:
        logger.error("FTP download failed for %s: %s", taxgroup, exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error downloading %s: %s", taxgroup, exc)
        return None


def parse_amr_metadata(
    content: io.StringIO,
    taxgroup_info: dict,
) -> dict[tuple[str, str, str, int], int]:
    """
    Parse the NCBI Pathogen Detection isolate-level AMR metadata TSV.

    Each row = one isolate. AMR genes are in the AMR_genotypes column as
    a comma-separated string e.g. "blaNDM-1,blaOXA-48,aac(6\')-Ib-cr".

    We parse each isolate row, extract its geographic and temporal metadata,
    split the AMR_genotypes field, and count isolates per
    (gene_name, pathogen_name, country_iso3, year).

    Args:
        content: StringIO of the TSV file
        taxgroup_info: Dict with taxgroup name and pathogen_name

    Returns:
        Dict mapping (gene_name, pathogen_name, country_iso3, year) → isolate_count
    """
    counts: dict[tuple[str, str, str, int], int] = defaultdict(int)

    header = None
    isolates_processed = 0
    isolates_skipped = 0
    gene_hits = 0

    for line in content:
        line = line.rstrip("\n")
        if not line:
            continue

        if header is None:
            # Strip leading # from first column name if present
            header = line.split("\t")
            if header and header[0].startswith("#"):
                header[0] = header[0][1:]
            continue

        fields = line.split("\t")
        if len(fields) < 10:
            isolates_skipped += 1
            continue

        row = dict(zip(header, fields))

        # Extract geography
        geo = row.get("geo_loc_name", "").strip()
        iso3 = get_iso3(geo)
        if not iso3:
            isolates_skipped += 1
            continue

        # Extract year
        collection_date = row.get("collection_date", "").strip()
        year = _parse_year(collection_date)
        if not year or year < 2010 or year > datetime.now().year:
            isolates_skipped += 1
            continue

        # Filter: skip obvious non-human sources
        isolation_source = row.get("isolation_source", "").strip().lower()
        host = row.get("host", "").strip().lower()
        source_type = row.get("source_type", "").strip().lower()

        # Skip environmental/food/animal isolates
        non_human_terms = [
            "soil", "water", "plant", "environment", "food", "poultry",
            "chicken", "cattle", "pig", "swine", "livestock", "bovine",
            "fish", "shellfish", "produce", "retail", "farm"
        ]
        if any(term in isolation_source for term in non_human_terms):
            isolates_skipped += 1
            continue
        if host and host not in ("homo sapiens", "human", ""):
            if any(term in host for term in ["gallus", "sus", "bos", "equus", "canis", "felis"]):
                isolates_skipped += 1
                continue

        # Parse AMR_genotypes field — comma-separated gene symbols
        amr_genotypes = row.get("AMR_genotypes", "").strip()
        if not amr_genotypes or amr_genotypes in ("NULL", "-", ""):
            isolates_skipped += 1
            continue

        isolates_processed += 1
        genes_in_isolate = [g.strip() for g in amr_genotypes.split(",") if g.strip()]

        for gene_symbol in genes_in_isolate:
            match = match_priority_gene(gene_symbol)
            if not match:
                continue
            gene_hits += 1
            key = (gene_symbol, taxgroup_info["pathogen_name"], iso3, year)
            counts[key] += 1

    logger.info(
        "%s: %d isolates processed, %d skipped, %d priority gene hits → %d unique signals",
        taxgroup_info["taxgroup"],
        isolates_processed,
        isolates_skipped,
        gene_hits,
        len(counts),
    )
    return dict(counts)


def _parse_year(date_str: str) -> Optional[int]:
    """
    Extract year from various NCBI date formats.
    Handles: YYYY, YYYY-MM, YYYY-MM-DD, MM/YYYY, 'missing', etc.

    Args:
        date_str: Raw date string from NCBI metadata

    Returns:
        Integer year or None if unparseable
    """
    if not date_str or date_str in ("missing", "not collected", "not applicable", "N/A", ""):
        return None

    # Try YYYY at start
    match = re.match(r"^(\d{4})", date_str)
    if match:
        year = int(match.group(1))
        if 1990 <= year <= 2030:
            return year

    # Try MM/YYYY
    match = re.match(r"^\d{1,2}/(\d{4})$", date_str)
    if match:
        year = int(match.group(1))
        if 1990 <= year <= 2030:
            return year

    return None


# ---------------------------------------------------------------------------
# Database writer
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = text("""
    CREATE TABLE IF NOT EXISTS genomic_signals (
        id              SERIAL PRIMARY KEY,
        gene_name       TEXT NOT NULL,
        gene_family     TEXT NOT NULL,
        drug_class      TEXT NOT NULL,
        pathogen_name   TEXT NOT NULL,
        country_iso3    TEXT NOT NULL,
        region_who      TEXT,
        year            INTEGER NOT NULL,
        isolate_count   INTEGER NOT NULL,
        data_source     TEXT DEFAULT 'NCBI_NDARO',
        last_updated    TIMESTAMP,
        CONSTRAINT uq_genomic_signal UNIQUE (gene_name, pathogen_name, country_iso3, year)
    )
""")

UPSERT_SQL = text("""
    INSERT INTO genomic_signals (
        gene_name, gene_family, drug_class, pathogen_name,
        country_iso3, region_who, year, isolate_count,
        data_source, last_updated
    )
    VALUES (
        :gene_name, :gene_family, :drug_class, :pathogen_name,
        :country_iso3, :region_who, :year, :isolate_count,
        :data_source, :last_updated
    )
    ON CONFLICT (gene_name, pathogen_name, country_iso3, year)
    DO UPDATE SET
        isolate_count = EXCLUDED.isolate_count,
        last_updated = EXCLUDED.last_updated
""")


def ensure_table(session_factory) -> None:
    """Create genomic_signals table if it doesn't exist."""
    with session_factory() as session:
        session.execute(CREATE_TABLE_SQL)
        session.commit()
    logger.info("genomic_signals table ready")


def write_genomic_signals(
    session_factory,
    counts: dict[tuple[str, str, str, int], int],
) -> int:
    """
    Upsert genomic signal counts into genomic_signals table.
    Uses INSERT ... ON CONFLICT DO UPDATE to keep counts current.

    Args:
        session_factory: SQLAlchemy sessionmaker
        counts: Dict mapping (gene_name, pathogen_name, country_iso3, year) → count

    Returns:
        Number of rows upserted
    """
    upserted = 0

    with session_factory() as session:
        for (gene_name, pathogen_name, iso3, year), count in counts.items():
            match = match_priority_gene(gene_name)
            if not match:
                continue
            gene_prefix, gene_info = match

            try:
                session.execute(
                    UPSERT_SQL,
                    {
                        "gene_name": gene_name,
                        "gene_family": gene_info["family"],
                        "drug_class": gene_info["drug_class"],
                        "pathogen_name": pathogen_name,
                        "country_iso3": iso3,
                        "region_who": WHO_REGION.get(iso3),
                        "year": year,
                        "isolate_count": count,
                        "data_source": "NCBI_NDARO",
                        "last_updated": datetime.utcnow(),
                    },
                )
                upserted += 1
            except Exception as exc:
                logger.error(
                    "Upsert failed for %s/%s/%s/%d: %s",
                    gene_name, pathogen_name, iso3, year, exc,
                )
                session.rollback()
                continue

        session.commit()

    return upserted


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_ndaro_ingestor(
    dry_run: bool = False,
    taxgroups_filter: Optional[list[str]] = None,
) -> dict:
    """
    Full pipeline: download AMR metadata from NCBI FTP → parse priority genes
    → aggregate by country/year → upsert into genomic_signals table.

    Args:
        dry_run: If True, parse but do not write to DB
        taxgroups_filter: Optional list of taxgroup names to process (default: all)

    Returns:
        Summary dict with counts per taxgroup and totals
    """
    logger.info("=== NDARO Genomic Ingestor starting ===")
    if dry_run:
        logger.info("DRY RUN — no database writes")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True) if not dry_run else None
    SessionFactory = sessionmaker(bind=engine) if engine else None

    if not dry_run and SessionFactory:
        ensure_table(SessionFactory)

    taxgroups_to_run = [
        tg for tg in TAXGROUPS
        if taxgroups_filter is None or tg["taxgroup"] in taxgroups_filter
    ]

    run_log = []
    total_signals = 0
    total_upserted = 0

    for tg in taxgroups_to_run:
        logger.info("--- Processing %s (%s) ---", tg["taxgroup"], tg["pathogen_name"])

        content = download_amr_metadata(tg["taxgroup"])
        if content is None:
            logger.error("Skipping %s — download failed", tg["taxgroup"])
            run_log.append({"taxgroup": tg["taxgroup"], "status": "download_failed"})
            continue

        counts = parse_amr_metadata(content, tg)
        total_signals += len(counts)

        if not dry_run and SessionFactory:
            upserted = write_genomic_signals(SessionFactory, counts)
        else:
            upserted = 0

        total_upserted += upserted

        # Top signals for this taxgroup (by count, descending)
        top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
        top_formatted = [
            {
                "gene": k[0], "pathogen": k[1],
                "country": k[2], "year": k[3], "count": v
            }
            for k, v in top
        ]

        entry = {
            "taxgroup": tg["taxgroup"],
            "pathogen": tg["pathogen_name"],
            "unique_signals": len(counts),
            "upserted": upserted,
            "top_signals": top_formatted,
            "status": "ok",
        }
        run_log.append(entry)

        logger.info(
            "%s: %d unique gene/country/year signals, %d upserted",
            tg["taxgroup"], len(counts), upserted,
        )

        # Polite delay between taxgroups
        time.sleep(1)

    summary = {
        "total_unique_signals": total_signals,
        "total_upserted": total_upserted,
        "taxgroups_processed": len(taxgroups_to_run),
        "dry_run": dry_run,
        "run_at": datetime.utcnow().isoformat(),
        "taxgroups": run_log,
    }

    # Save run log
    with open(RUN_LOG, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    logger.info("Run log saved to %s", RUN_LOG)

    logger.info("=== NDARO Genomic Ingestor complete ===")
    logger.info(
        "Total: %d unique genomic signals | %d upserted | %d taxgroups",
        total_signals, total_upserted, len(taxgroups_to_run),
    )

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Ingest NCBI NDARO genomic resistance gene signals"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report but do not write to database",
    )
    parser.add_argument(
        "--taxgroup",
        type=str,
        default=None,
        help="Process only this taxgroup (e.g. Klebsiella). Default: all.",
    )
    args = parser.parse_args()

    taxgroups_filter = [args.taxgroup] if args.taxgroup else None

    summary = run_ndaro_ingestor(dry_run=args.dry_run, taxgroups_filter=taxgroups_filter)

    print("\n=== SUMMARY ===")
    print(f"  Total unique signals : {summary['total_unique_signals']}")
    print(f"  Total upserted       : {summary['total_upserted']}")
    print(f"  Taxgroups processed  : {summary['taxgroups_processed']}")
    print(f"  Dry run              : {summary['dry_run']}")
    print()
    for tg in summary["taxgroups"]:
        print(f"  {tg['taxgroup']}: {tg.get('unique_signals', 0)} signals, status={tg['status']}")
        for sig in tg.get("top_signals", [])[:5]:
            print(
                f"    {sig['gene']:<20} {sig['country']} {sig['year']}  n={sig['count']}"
            )