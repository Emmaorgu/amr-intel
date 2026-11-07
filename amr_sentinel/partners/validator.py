"""
amr_sentinel/partners/validator.py

Stateless dry-run validator for hospital / laboratory submissions.

A lab uploads a CSV or tab-delimited file (plain CSV template or a WHONET
export). The validator parses it, refuses it outright if it contains patient
identifiers, validates every isolate and result against the data contract, and
reports which pathogen x antibiotic x quarter groups would reach the minimum
sample size for aggregation.

Inputs:  raw file bytes (and an optional filename for logging only)
Outputs: a JSON-serialisable report dict (see validate_submission)
Dependencies: standard library + amr_sentinel.partners.contract

Guarantees:
  * Writes nothing: no database, no disk, no network.
  * Never echoes cell values back. Reports contain column names, row numbers,
    counts and reasons only, so a refused file never leaks identifiers.
  * Never logs file content; logs counts only.
  * Never invents interpretations: numeric MIC / zone values without an S/I/R
    are reported, not interpreted.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from amr_sentinel.partners.contract import (
    AGE_BANDS,
    ANTIBIOTIC_CODES,
    ANTIBIOTIC_NAME_TO_CODE,
    CANONICAL_PATHOGENS,
    CONTRACT_VERSION,
    INTERPRETATION_MAP,
    MIN_COLLECTION_YEAR,
    MIN_ISOLATES_PER_GROUP,
    NOT_TESTED_VALUES,
    S_AUREUS_ONLY_CODES,
    SEX_VALUES,
)

logger = logging.getLogger(__name__)

MAX_REJECT_SAMPLES = 25
MAX_GROUPS_RETURNED = 200
MAX_ROW_NUMBERS_PER_HIT = 5

# ── Header roles ─────────────────────────────────────────────────────────────
ROLE_ALIASES: Dict[str, Set[str]] = {
    "organism": {"ORGANISM", "ORGANISM_CODE", "ORG", "PATHOGEN"},
    "date": {"SPEC_DATE", "COLLECTION_DATE", "SPECIMEN_DATE", "DATE"},
    "specimen": {"SPEC_TYPE", "SPECIMEN_TYPE", "SPECIMEN"},
    "ward": {"WARD_TYPE", "PATIENT_TYPE"},
    "age": {"AGE_BAND", "AGE"},
    "sex": {"SEX", "GENDER"},
    "antibiotic": {"ANTIBIOTIC", "DRUG"},
    "interp": {"INTERPRETATION", "SIR", "RESULT"},
}

# WHONET columns that are recognised but deliberately not used.
IGNORED_COLUMNS: Set[str] = {
    "INSTITUT", "DEPARTMENT", "LOCATION", "WARD", "SPEC_NUM", "SPEC_CODE",
    "LAB", "LAB_NUM", "ORG_TYPE", "COUNTRY", "ISOLATE_NUM", "COMMENT",
}

IDENT_EXACT: Set[str] = {
    "FIRST_NAME", "LAST_NAME", "SURNAME", "FORENAME", "NAME", "PATIENT_NAME",
    "FULL_NAME", "MIDDLE_NAME", "DATE_BIRTH", "DOB", "DATE_OF_BIRTH",
    "BIRTH_DATE", "BIRTHDATE", "PHONE", "TELEPHONE", "TEL", "MOBILE",
    "PHONE_NUMBER", "EMAIL", "E_MAIL", "ADDRESS", "STREET", "NIN",
    "NATIONAL_ID", "HOSPITAL_NUMBER", "HOSPITAL_NO", "HOSP_NO", "HOSP_NUM",
    "MRN", "FOLDER_NO", "FOLDER_NUMBER", "CARD_NO", "FILE_NO", "NHIS",
    "NHIS_NO", "FOLIO",
}
IDENT_TOKEN_RE = re.compile(r"(^|_)(PHONE|MOBILE|EMAIL|ADDRESS|DOB|NIN|MRN)(_|$)")
IDENT_NAME_RE = re.compile(r"(FIRST|LAST|MIDDLE|FULL|PATIENT|SUR|FORE)_?NAME")

HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,128}$")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE_RE = re.compile(r"^\+?\d{10,15}$")

# WHONET antibiotic column shapes.
RAW_COL_RE = re.compile(r"^([A-Z]{3})_N[DME]\d*$")
INTERP_COL_RE = re.compile(r"^([A-Z]{3})(?:_N[DME]\d*)?_(?:I|RIS|SIR|INTERP)$")

DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y")


# ── Small helpers ────────────────────────────────────────────────────────────
def norm_header(raw: str) -> str:
    """Normalise a header cell: strip, upper-case, spaces/hyphens to underscores."""
    return re.sub(r"[\s\-]+", "_", (raw or "").strip().upper())


def decode_bytes(raw: bytes) -> Tuple[str, List[str]]:
    """Decode uploaded bytes to text. Returns (text, warnings)."""
    warnings: List[str] = []
    try:
        return raw.decode("utf-8-sig"), warnings
    except UnicodeDecodeError:
        warnings.append("File is not UTF-8; decoded as Latin-1. Re-export as UTF-8 if text looks wrong.")
        return raw.decode("latin-1"), warnings


def looks_binary(raw: bytes) -> bool:
    """True for zip-based formats (xlsx) or files containing NUL bytes."""
    return raw[:4] == b"PK\x03\x04" or b"\x00" in raw[:2048]


def sniff_delimiter(text: str) -> str:
    """Pick the delimiter (comma, tab, semicolon, pipe) from the header line."""
    first = text.split("\n", 1)[0]
    counts = {d: first.count(d) for d in (",", "\t", ";", "|")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def parse_date(value: str) -> Tuple[Optional[date], Optional[str]]:
    """
    Parse a collection date. Returns (date, None) or (None, reason_code).

    Reason codes: missing_collection_date, invalid_collection_date,
    collection_date_in_future, collection_date_too_old.
    """
    v = (value or "").strip()
    if not v:
        return None, "missing_collection_date"
    v = re.split(r"[T ]", v, maxsplit=1)[0]
    parsed: Optional[date] = None
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(v, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        return None, "invalid_collection_date"
    if parsed > date.today():
        return None, "collection_date_in_future"
    if parsed.year < MIN_COLLECTION_YEAR:
        return None, "collection_date_too_old"
    return parsed, None


def quarter_of(d: date) -> str:
    """Return a quarter label such as '2025-Q3'."""
    return str(d.year) + "-Q" + str((d.month - 1) // 3 + 1)


def map_organism(value: str) -> Optional[str]:
    """Map a WHONET code or organism name to a canonical pathogen, else None."""
    return CANONICAL_PATHOGENS.get((value or "").strip().lower())


def map_specimen(value: str) -> str:
    """Map free-text specimen type to a category (blood, urine, ..., other)."""
    v = (value or "").strip().lower()
    rules = (
        ("blood", ("blood", "bl", "bc")),
        ("urine", ("urine", "ur", "mid-stream", "msu")),
        ("csf", ("csf", "cerebrospinal")),
        ("wound", ("wound", "pus", "swab", "abscess", "skin")),
        ("respiratory", ("sputum", "respiratory", "bronch", "tracheal", "throat")),
        ("stool", ("stool", "faec", "fec", "rectal")),
    )
    for category, keys in rules:
        for k in keys:
            if v == k or (len(k) > 3 and k in v):
                return category
    return "other"


def map_ward(value: str) -> str:
    """Map free-text ward / patient type to ICU, inpatient, outpatient, ED or unknown."""
    v = (value or "").strip().lower()
    if not v:
        return "unknown"
    if "icu" in v or "intensive" in v:
        return "ICU"
    if v in ("ed", "a&e", "er") or "emerg" in v or "casualty" in v:
        return "ED"
    if v in ("out", "op") or "outpat" in v:
        return "outpatient"
    if v in ("in", "ip") or "inpat" in v or "ward" in v:
        return "inpatient"
    return "unknown"


def map_age_band(value: str) -> Optional[str]:
    """Map a numeric age or band string to a contract age band, else None."""
    v = (value or "").strip().replace("\u2013", "-").replace("\u2014", "-").replace(" ", "")
    if not v:
        return None
    if v in AGE_BANDS:
        return v
    try:
        age = int(float(v))
    except ValueError:
        return None
    if age < 0 or age > 120:
        return None
    if age <= 4:
        return "0-4"
    if age <= 14:
        return "5-14"
    if age <= 44:
        return "15-44"
    if age <= 64:
        return "45-64"
    return "65+"


# ── Identifier detection ─────────────────────────────────────────────────────
def header_identifier_reason(name: str) -> Optional[str]:
    """Return a reason string if a normalised header names a direct identifier."""
    if name in IDENT_EXACT:
        return "Column name indicates a direct patient identifier."
    if IDENT_TOKEN_RE.search(name) or IDENT_NAME_RE.search(name):
        return "Column name indicates a direct patient identifier."
    return None


def scan_cell_for_identifier(value: str) -> Optional[str]:
    """Return 'email' or 'phone_or_long_number' if a cell looks identifying, else None."""
    v = value.strip()
    if not v:
        return None
    if EMAIL_RE.search(v):
        return "email"
    if PHONE_RE.match(re.sub(r"[\s\-()]", "", v)):
        return "phone_or_long_number"
    return None


# ── Report scaffolding ───────────────────────────────────────────────────────
def _empty_report(status: str, message: str) -> Dict[str, Any]:
    """Build a minimal report for early exits."""
    return {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "message": message,
        "format": None,
        "delimiter": None,
        "identifiers": {"found": False, "columns": [], "content_hits": []},
        "columns": {"recognised": [], "ignored": [], "unmapped": [], "numeric_only": []},
        "counts": {
            "isolates_total": 0, "isolates_rejected": 0, "isolates_after_dedup": 0,
            "results_accepted": 0, "results_rejected": 0,
            "results_not_tested": 0, "results_superseded": 0,
        },
        "reject_reasons": [],
        "reject_samples": [],
        "dedup": {"token_present": False, "duplicates_removed": 0},
        "groups": [],
        "groups_summary": {"total": 0, "eligible": 0, "below_threshold": 0,
                           "min_isolates": MIN_ISOLATES_PER_GROUP},
        "warnings": [],
    }


def validate_submission(raw: bytes, filename: Optional[str] = None) -> Dict[str, Any]:
    """
    Dry-run validate a facility submission.

    Args:
        raw: Uploaded file bytes (CSV / TSV).
        filename: Original filename, used for logging only.

    Returns:
        Report dict with keys: contract_version, status, message, format,
        delimiter, identifiers, columns, counts, reject_reasons,
        reject_samples, dedup, groups, groups_summary, warnings.

        status is one of: accepted, accepted_with_rejects, rejected,
        refused_identifiers, unrecognised_format, empty, binary_file.
    """
    if not raw or not raw.strip():
        return _empty_report("empty", "The file is empty.")
    if looks_binary(raw):
        return _empty_report(
            "binary_file",
            "This looks like a binary file (e.g. .xlsx). Export it as CSV or tab-delimited text and retry.",
        )

    text, warnings = decode_bytes(raw)
    delimiter = sniff_delimiter(text)
    try:
        table = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)
                 if any(c.strip() for c in r)]
    except csv.Error as exc:
        logger.warning("partner validate: csv parse error: %s", exc)
        return _empty_report("unrecognised_format", "The file could not be parsed as delimited text.")
    if len(table) < 2:
        return _empty_report("empty", "The file has a header but no data rows.")

    headers_raw = [h.strip() for h in table[0]]
    headers = [norm_header(h) for h in headers_raw]
    data = table[1:]
    delim_name = {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}[delimiter]

    # ── Role mapping ─────────────────────────────────────────────────────────
    roles: Dict[str, int] = {}
    for idx, name in enumerate(headers):
        for role, aliases in ROLE_ALIASES.items():
            if name in aliases and role not in roles:
                roles[role] = idx

    token_idx: Optional[int] = None
    for idx, name in enumerate(headers):
        if name in ("PATIENT_TOKEN", "PATIENT_ID") and token_idx is None:
            token_idx = idx

    # ── Identifier detection (headers, token shape, cell content) ───────────
    ident_columns: List[Dict[str, str]] = []
    for idx, name in enumerate(headers):
        if idx == token_idx:
            continue
        reason = header_identifier_reason(name)
        if reason:
            ident_columns.append({"column": headers_raw[idx], "reason": reason})

    token_ok = False
    if token_idx is not None:
        vals = [r[token_idx].strip() for r in data if token_idx < len(r) and r[token_idx].strip()]
        if vals and all(HEX_TOKEN_RE.match(v) for v in vals):
            token_ok = True
        elif vals:
            ident_columns.append({
                "column": headers_raw[token_idx],
                "reason": "Values are not a 32-128 character hex digest. Raw patient IDs are refused; "
                          "hash them at the lab (HMAC-SHA256 with a facility-held salt).",
            })

    skip_scan: Set[int] = set()
    if token_idx is not None:
        skip_scan.add(token_idx)
    if "date" in roles:
        skip_scan.add(roles["date"])
    hits: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    hit_counts: Counter = Counter()
    for rnum, row in enumerate(data, start=1):
        for cidx, cell in enumerate(row):
            if cidx in skip_scan or cidx >= len(headers):
                continue
            kind = scan_cell_for_identifier(cell)
            if kind:
                key = (headers_raw[cidx], kind)
                hit_counts[key] += 1
                if len(hits[key]) < MAX_ROW_NUMBERS_PER_HIT:
                    hits[key].append(rnum)
    content_hits = [
        {"column": col, "kind": kind, "count": hit_counts[(col, kind)], "example_rows": rows}
        for (col, kind), rows in hits.items()
    ]

    if ident_columns or content_hits:
        report = _empty_report(
            "refused_identifiers",
            "File refused: it contains patient identifiers. AMR-Intel accepts isolate-level "
            "surveillance fields only. Remove the flagged columns, tokenise any patient ID, and resubmit.",
        )
        report["delimiter"] = delim_name
        report["identifiers"] = {"found": True, "columns": ident_columns, "content_hits": content_hits}
        report["counts"]["isolates_total"] = len(data)
        logger.info("partner validate refused: %d identifier columns, %d content hits",
                    len(ident_columns), len(content_hits))
        return report

    # ── Format detection ─────────────────────────────────────────────────────
    interp_cols: Dict[str, List[int]] = defaultdict(list)
    raw_cols: Dict[str, List[int]] = defaultdict(list)
    unmapped: List[str] = []
    recognised: List[str] = []
    ignored: List[str] = []
    role_idx = set(roles.values())

    for idx, name in enumerate(headers):
        if idx == token_idx or idx in role_idx:
            recognised.append(headers_raw[idx])
            continue
        if name in IGNORED_COLUMNS:
            ignored.append(headers_raw[idx])
            continue
        m_i = INTERP_COL_RE.match(name)
        if m_i and m_i.group(1) in ANTIBIOTIC_CODES:
            interp_cols[m_i.group(1)].append(idx)
            recognised.append(headers_raw[idx])
            continue
        if name in ANTIBIOTIC_CODES:
            interp_cols[name].append(idx)
            recognised.append(headers_raw[idx])
            continue
        m_r = RAW_COL_RE.match(name)
        if m_r and m_r.group(1) in ANTIBIOTIC_CODES:
            raw_cols[m_r.group(1)].append(idx)
            continue
        unmapped.append(headers_raw[idx])

    numeric_only = sorted(code for code in raw_cols if code not in interp_cols)
    for code, idxs in raw_cols.items():
        if code in interp_cols:
            ignored.extend(headers_raw[i] for i in idxs)
        else:
            recognised.extend(headers_raw[i] for i in idxs)

    is_long = all(k in roles for k in ("organism", "antibiotic", "interp"))
    is_wide = "organism" in roles and bool(interp_cols)
    base_cols = {"recognised": recognised, "ignored": ignored,
                 "unmapped": unmapped, "numeric_only": numeric_only}

    if not (is_long or is_wide) or "date" not in roles or "specimen" not in roles:
        missing = [lbl for key, lbl in (("organism", "organism"), ("date", "collection_date"),
                                        ("specimen", "specimen_type")) if key not in roles]
        report = _empty_report(
            "unrecognised_format",
            "The file does not match the plain CSV template or a WHONET export. "
            + ("Missing required columns: " + ", ".join(missing) + ". " if missing else "")
            + ("No antibiotic result columns (or antibiotic / interpretation columns) were found. "
               if not (is_long or is_wide) else "")
            + ("Numeric-only WHONET columns were found; supply S/I/R interpretations." if numeric_only else ""),
        )
        report["delimiter"] = delim_name
        report["columns"] = base_cols
        report["counts"]["isolates_total"] = len(data)
        return report

    fmt = "template_csv" if is_long else "whonet_wide"
    if numeric_only:
        warnings.append(
            "Numeric MIC / zone columns without an S/I/R interpretation were ignored (" + ", ".join(numeric_only)
            + "). v1 does not interpret breakpoints."
        )

    # ── Build isolate units ──────────────────────────────────────────────────
    def cell(row: List[str], idx: Optional[int]) -> str:
        """Safe cell accessor."""
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    # Each unit: dict(rows, token, date_raw, org_raw, spec_raw, ward_raw, age_raw, sex_raw, results[(code_or_name, value)])
    units: List[Dict[str, Any]] = []
    if is_wide:
        for rnum, row in enumerate(data, start=1):
            results: List[Tuple[str, str]] = []
            for code, idxs in interp_cols.items():
                value = ""
                for i in idxs:
                    value = cell(row, i)
                    if value:
                        break
                results.append((code, value))
            units.append({
                "rows": [rnum], "token": cell(row, token_idx),
                "date_raw": cell(row, roles.get("date")), "org_raw": cell(row, roles.get("organism")),
                "spec_raw": cell(row, roles.get("specimen")), "ward_raw": cell(row, roles.get("ward")),
                "age_raw": cell(row, roles.get("age")), "sex_raw": cell(row, roles.get("sex")),
                "results": results,
            })
    else:
        grouped: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        for rnum, row in enumerate(data, start=1):
            token = cell(row, token_idx)
            key: Tuple[str, ...]
            if token_idx is not None and token:
                key = (token, cell(row, roles["date"]), cell(row, roles["organism"]).lower(),
                       cell(row, roles["specimen"]).lower())
            else:
                key = ("__row__", str(rnum))
            unit = grouped.get(key)
            if unit is None:
                unit = {
                    "rows": [], "token": token,
                    "date_raw": cell(row, roles["date"]), "org_raw": cell(row, roles["organism"]),
                    "spec_raw": cell(row, roles["specimen"]), "ward_raw": cell(row, roles.get("ward")),
                    "age_raw": cell(row, roles.get("age")), "sex_raw": cell(row, roles.get("sex")),
                    "results": [],
                }
                grouped[key] = unit
            unit["rows"].append(rnum)
            unit["results"].append((cell(row, roles["antibiotic"]), cell(row, roles["interp"])))
        units = list(grouped.values())
        if token_idx is None:
            warnings.append(
                "No patient_token column: isolates cannot be reconstructed or de-duplicated, "
                "so counts are per result row and cannot be certified as first-isolate."
            )

    # ── Validate units ───────────────────────────────────────────────────────
    isolate_reasons: Counter = Counter()
    result_reasons: Counter = Counter()
    samples: List[Dict[str, Any]] = []
    not_tested = 0
    superseded = 0
    isolates_rejected = 0
    results_rejected = 0
    warn_counter: Counter = Counter()
    unmapped_orgs: Counter = Counter()
    unmapped_abx: Counter = Counter()

    def add_sample(rows: List[int], reason: str) -> None:
        """Record a reject sample (row number + reason only, never values)."""
        if len(samples) < MAX_REJECT_SAMPLES:
            samples.append({"row": rows[0], "reason": reason})

    valid: List[Dict[str, Any]] = []
    for u in units:
        reason: Optional[str] = None
        d: Optional[date] = None
        pathogen = map_organism(u["org_raw"])
        if not u["org_raw"]:
            reason = "missing_organism"
        elif pathogen is None:
            reason = "organism_not_in_scope"
            unmapped_orgs[u["org_raw"][:40]] += 1
        else:
            d, date_reason = parse_date(u["date_raw"])
            if date_reason:
                reason = date_reason
            elif not u["spec_raw"]:
                reason = "missing_specimen_type"

        tested = [(a, v) for a, v in u["results"] if v.strip().upper() not in NOT_TESTED_VALUES]
        not_tested += len(u["results"]) - len(tested)

        if reason:
            isolates_rejected += 1
            isolate_reasons[reason] += 1
            add_sample(u["rows"], reason)
            results_rejected += len(tested)
            continue
        assert d is not None and pathogen is not None

        # Optional demographic fields: warnings only.
        if u["ward_raw"] and map_ward(u["ward_raw"]) == "unknown":
            warn_counter["ward_type"] += 1
        if u["age_raw"] and map_age_band(u["age_raw"]) is None:
            warn_counter["age_band"] += 1
        if u["sex_raw"] and u["sex_raw"].strip().upper() not in SEX_VALUES:
            warn_counter["sex"] += 1
        if map_specimen(u["spec_raw"]) == "other" and u["spec_raw"].strip().lower() != "other":
            warn_counter["specimen_type"] += 1

        accepted: Dict[str, str] = {}
        sources: Dict[str, str] = {}
        ordered = sorted(tested, key=lambda av: 0 if av[0].strip().upper() == "FOX" else 1)
        for abx_raw, interp_raw in ordered:
            code = abx_raw.strip().upper()
            if code not in ANTIBIOTIC_CODES:
                code = ANTIBIOTIC_NAME_TO_CODE.get(abx_raw.strip().lower(), "")
            if not code:
                results_rejected += 1
                result_reasons["antibiotic_not_in_scope"] += 1
                unmapped_abx[abx_raw[:40]] += 1
                add_sample(u["rows"], "antibiotic_not_in_scope")
                continue
            if code in S_AUREUS_ONLY_CODES and pathogen != "S. aureus":
                results_rejected += 1
                result_reasons["oxacillin_cefoxitin_only_mapped_for_s_aureus"] += 1
                add_sample(u["rows"], "oxacillin_cefoxitin_only_mapped_for_s_aureus")
                continue
            sir = INTERPRETATION_MAP.get(interp_raw.strip().upper())
            if sir is None:
                results_rejected += 1
                result_reasons["invalid_interpretation"] += 1
                add_sample(u["rows"], "invalid_interpretation")
                continue
            name = ANTIBIOTIC_CODES[code]
            if name in accepted:
                if name == "Methicillin" and sources.get(name) in S_AUREUS_ONLY_CODES and code in S_AUREUS_ONLY_CODES:
                    superseded += 1
                else:
                    results_rejected += 1
                    result_reasons["duplicate_result_for_isolate"] += 1
                    add_sample(u["rows"], "duplicate_result_for_isolate")
                continue
            accepted[name] = sir
            sources[name] = code

        if not accepted:
            continue
        valid.append({"token": u["token"], "date": d, "pathogen": pathogen,
                      "row": u["rows"][0], "results": accepted})

    # ── De-duplicate: first isolate per patient per organism per quarter ────
    dup_removed = 0
    token_present = token_ok
    if token_present:
        valid.sort(key=lambda x: (x["date"], x["row"]))
        seen: Set[Tuple[str, str, str]] = set()
        kept: List[Dict[str, Any]] = []
        untokenised = 0
        for iso in valid:
            if not iso["token"]:
                untokenised += 1
                kept.append(iso)
                continue
            k = (iso["token"].lower(), iso["pathogen"], quarter_of(iso["date"]))
            if k in seen:
                dup_removed += 1
                continue
            seen.add(k)
            kept.append(iso)
        valid = kept
        if untokenised:
            warnings.append(str(untokenised) + " isolate(s) had no patient token and were not de-duplicated.")

    # ── Group counts ─────────────────────────────────────────────────────────
    stats: Dict[Tuple[str, str, str], Dict[str, int]] = defaultdict(lambda: {"n": 0, "S": 0, "I": 0, "R": 0})
    results_accepted = 0
    for iso in valid:
        q = quarter_of(iso["date"])
        for abx, sir in iso["results"].items():
            g = stats[(iso["pathogen"], abx, q)]
            g["n"] += 1
            g[sir] += 1
            results_accepted += 1

    groups: List[Dict[str, Any]] = []
    for (pathogen, abx, q), g in stats.items():
        eligible = g["n"] >= MIN_ISOLATES_PER_GROUP
        entry: Dict[str, Any] = {"pathogen": pathogen, "antibiotic": abx, "quarter": q,
                                 "n": g["n"], "eligible": eligible}
        if eligible:
            entry["resistance_pct"] = round(100.0 * g["R"] / g["n"], 1)
            if g["R"] == g["n"]:
                entry["flag"] = "100% resistance would be rejected by the plausibility rule"
        groups.append(entry)
    groups.sort(key=lambda e: (not e["eligible"], -e["n"], e["pathogen"], e["antibiotic"], e["quarter"]))
    eligible_ct = sum(1 for e in groups if e["eligible"])

    # ── Warnings ─────────────────────────────────────────────────────────────
    for fld, n in warn_counter.items():
        warnings.append(str(n) + " isolate(s) had an unrecognised " + fld + " value (treated as unknown / other).")
    if unmapped_orgs:
        top = ", ".join(k + " (" + str(v) + ")" for k, v in unmapped_orgs.most_common(5))
        warnings.append("Organisms outside the six tracked pathogens: " + top + ".")
    if unmapped_abx:
        top = ", ".join(k + " (" + str(v) + ")" for k, v in unmapped_abx.most_common(5))
        warnings.append("Antibiotics not in the v1 mapping: " + top + ".")
    if unmapped:
        warnings.append("Columns not recognised and not used: " + ", ".join(unmapped) + ".")

    reasons = (
        [{"reason": r, "level": "isolate", "count": c} for r, c in isolate_reasons.most_common()]
        + [{"reason": r, "level": "result", "count": c} for r, c in result_reasons.most_common()]
    )

    if results_accepted == 0:
        status = "rejected"
        message = "No results passed validation."
    elif isolates_rejected or results_rejected:
        status = "accepted_with_rejects"
        message = "Valid results found; some rows were rejected. See reasons below."
    else:
        status = "accepted"
        message = "All rows passed validation."

    report = _empty_report(status, message)
    report.update({
        "format": fmt,
        "delimiter": delim_name,
        "columns": base_cols,
        "counts": {
            "isolates_total": len(units),
            "isolates_rejected": isolates_rejected,
            "isolates_after_dedup": len(valid),
            "results_accepted": results_accepted,
            "results_rejected": results_rejected,
            "results_not_tested": not_tested,
            "results_superseded": superseded,
        },
        "reject_reasons": reasons,
        "reject_samples": samples,
        "dedup": {"token_present": token_present, "duplicates_removed": dup_removed},
        "groups": groups[:MAX_GROUPS_RETURNED],
        "groups_summary": {"total": len(groups), "eligible": eligible_ct,
                           "below_threshold": len(groups) - eligible_ct,
                           "min_isolates": MIN_ISOLATES_PER_GROUP},
        "warnings": warnings,
    })
    logger.info("partner validate %s: status=%s units=%d accepted=%d rejected=%d eligible_groups=%d",
                filename or "upload", status, len(units), results_accepted, results_rejected, eligible_ct)
    return report
