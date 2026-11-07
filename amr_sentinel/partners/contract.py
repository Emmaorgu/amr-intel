"""
amr_sentinel/partners/contract.py

Versioned data contract for facility (hospital / laboratory) submissions.

This module is pure data: field definitions, code maps, thresholds and the
downloadable CSV template. It has no database or network dependency, so the
dry-run validator, the API router and the dashboard all read the same source
of truth.

Inputs:  none
Outputs: CONTRACT_VERSION, field specs, mapping tables, template text,
         get_contract() for the API.
Dependencies: standard library only.

IMPORTANT (mapping tables): the WHONET codes below follow the common WHONET
BacLink conventions but have NOT yet been verified against a real Nigerian
lab export. Treat them as v1 and correct them against the first pilot file.
Canonical pathogen / antibiotic names must stay aligned with the strings used
in resistance_records (see CANONICAL_PATHOGENS / CANONICAL_ANTIBIOTICS).
"""

from __future__ import annotations

from typing import Dict, List

CONTRACT_VERSION = "1.0.0"

# Minimum first-isolate count for a facility x pathogen x antibiotic x quarter
# group to be eligible for aggregation (CLSI M39 practice is n >= 30).
MIN_ISOLATES_PER_GROUP = 30

# Hard upload ceiling for the dry-run validator.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Earliest plausible collection date for a submission.
MIN_COLLECTION_YEAR = 2000

# ── Canonical pathogens ──────────────────────────────────────────────────────
# Key = lower-case synonym or WHONET organism code. Value = canonical name.
CANONICAL_PATHOGENS: Dict[str, str] = {
    "eco": "E. coli",
    "escherichia coli": "E. coli",
    "e. coli": "E. coli",
    "e.coli": "E. coli",
    "kpn": "K. pneumoniae",
    "klebsiella pneumoniae": "K. pneumoniae",
    "k. pneumoniae": "K. pneumoniae",
    "sau": "S. aureus",
    "staphylococcus aureus": "S. aureus",
    "s. aureus": "S. aureus",
    "pae": "Pseudomonas",
    "pseudomonas aeruginosa": "Pseudomonas",
    "pseudomonas": "Pseudomonas",
    "aba": "Acinetobacter",
    "acinetobacter baumannii": "Acinetobacter",
    "acinetobacter": "Acinetobacter",
    "spn": "S. pneumoniae",
    "streptococcus pneumoniae": "S. pneumoniae",
    "s. pneumoniae": "S. pneumoniae",
}

# ── Canonical antibiotics ────────────────────────────────────────────────────
# WHONET 3-letter code -> canonical antibiotic name.
ANTIBIOTIC_CODES: Dict[str, str] = {
    "AMP": "Ampicillin",
    "CRO": "Ceftriaxone",
    "CTX": "Cefotaxime",
    "CAZ": "Ceftazidime",
    "IPM": "Imipenem",
    "MEM": "Meropenem",
    "ETP": "Ertapenem",
    "CIP": "Ciprofloxacin",
    "LVX": "Levofloxacin",
    "GEN": "Gentamicin",
    "AMK": "Amikacin",
    "COL": "Colistin",
    "TZP": "Piperacillin-tazobactam",
    "SXT": "Trimethoprim-sulfamethoxazole",
    "PEN": "Penicillin",
    "OXA": "Methicillin",
    "FOX": "Methicillin",
}

# Codes that map to "Methicillin" (MRSA inference) and are only meaningful for
# S. aureus. For any other organism these results are rejected, not guessed.
S_AUREUS_ONLY_CODES = {"OXA", "FOX"}

# Lower-case antibiotic name / synonym -> WHONET code (long-format input).
ANTIBIOTIC_NAME_TO_CODE: Dict[str, str] = {
    "ampicillin": "AMP",
    "ceftriaxone": "CRO",
    "cefotaxime": "CTX",
    "ceftazidime": "CAZ",
    "imipenem": "IPM",
    "meropenem": "MEM",
    "ertapenem": "ETP",
    "ciprofloxacin": "CIP",
    "levofloxacin": "LVX",
    "gentamicin": "GEN",
    "amikacin": "AMK",
    "colistin": "COL",
    "piperacillin-tazobactam": "TZP",
    "piperacillin/tazobactam": "TZP",
    "trimethoprim-sulfamethoxazole": "SXT",
    "co-trimoxazole": "SXT",
    "penicillin": "PEN",
    "oxacillin": "OXA",
    "methicillin": "OXA",
    "cefoxitin": "FOX",
}

CANONICAL_ANTIBIOTICS = sorted(set(ANTIBIOTIC_CODES.values()))

# ── Interpretation values ────────────────────────────────────────────────────
INTERPRETATION_MAP: Dict[str, str] = {
    "S": "S", "SUSCEPTIBLE": "S",
    "I": "I", "INTERMEDIATE": "I", "SDD": "I",
    "R": "R", "RESISTANT": "R",
}
# Values meaning "not tested": skipped silently and counted separately.
NOT_TESTED_VALUES = {"", "ND", "NA", "N/A", "-", "NT", "NOT TESTED"}

AGE_BANDS: List[str] = ["0-4", "5-14", "15-44", "45-64", "65+"]
SEX_VALUES = {"M": "M", "MALE": "M", "F": "F", "FEMALE": "F", "U": "U", "UNKNOWN": "U"}
WARD_TYPES = ["ICU", "inpatient", "outpatient", "ED", "unknown"]
SPECIMEN_TYPES = ["blood", "urine", "csf", "wound", "respiratory", "stool", "other"]

# ── Long-format template ─────────────────────────────────────────────────────
TEMPLATE_COLUMNS: List[str] = [
    "patient_token",
    "collection_date",
    "organism",
    "specimen_type",
    "ward_type",
    "age_band",
    "sex",
    "antibiotic",
    "interpretation",
]


def template_csv() -> str:
    """Return the downloadable CSV template: header row only, no example rows."""
    return ",".join(TEMPLATE_COLUMNS) + "\n"


def get_contract() -> Dict[str, object]:
    """Return the full data contract as a JSON-serialisable dict for the API."""
    fields = [
        {"name": "patient_token", "required": False,
         "format": "Hex digest, 32-128 chars (HMAC-SHA256 of the hospital ID)",
         "notes": "Used only to keep the first isolate per patient per organism per quarter. "
                  "Without it, counts cannot be certified as first-isolate."},
        {"name": "collection_date", "required": True,
         "format": "YYYY-MM-DD (day-first DD/MM/YYYY also accepted)",
         "notes": "Not in the future; not before " + str(MIN_COLLECTION_YEAR) + "."},
        {"name": "organism", "required": True,
         "format": "WHONET code (eco, kpn, sau, pae, aba, spn) or full name",
         "notes": "Only the six tracked pathogens are in scope in v1."},
        {"name": "specimen_type", "required": True,
         "format": "Free text mapped to: " + ", ".join(SPECIMEN_TYPES),
         "notes": "Unrecognised text is kept as 'other' with a warning."},
        {"name": "ward_type", "required": False,
         "format": ", ".join(WARD_TYPES), "notes": "Unrecognised values become 'unknown'."},
        {"name": "age_band", "required": False,
         "format": ", ".join(AGE_BANDS) + " (numeric AGE also accepted and banded)",
         "notes": "Date of birth is never accepted."},
        {"name": "sex", "required": False, "format": "M, F, U", "notes": ""},
        {"name": "antibiotic", "required": True,
         "format": "WHONET 3-letter code or antibiotic name",
         "notes": "One row per isolate-antibiotic result in the plain CSV template."},
        {"name": "interpretation", "required": True,
         "format": "S, I, R (SDD is counted as I)",
         "notes": "Numeric MIC / zone values alone are not interpreted in v1: "
                  "the lab must supply S/I/R under its own breakpoint standard."},
    ]
    return {
        "contract_version": CONTRACT_VERSION,
        "min_isolates_per_group": MIN_ISOLATES_PER_GROUP,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "formats": [
            {"id": "template_csv", "label": "Plain CSV template (long format)",
             "description": "One row per isolate-antibiotic result."},
            {"id": "whonet_wide", "label": "WHONET export (wide format)",
             "description": "One row per isolate; antibiotic columns by WHONET code. "
                            "Export as CSV or tab-delimited text."},
        ],
        "fields": fields,
        "pathogens": sorted(set(CANONICAL_PATHOGENS.values())),
        "antibiotics": CANONICAL_ANTIBIOTICS,
        "template_columns": TEMPLATE_COLUMNS,
        "never_accepted": [
            "Patient names", "Date of birth", "Phone numbers", "Email addresses",
            "Addresses", "National ID numbers", "Raw hospital / folder numbers",
        ],
        "tokenisation_recipe": (
            "token = HMAC_SHA256(key=facility_secret_salt, msg=hospital_number).hexdigest()"
        ),
    }
