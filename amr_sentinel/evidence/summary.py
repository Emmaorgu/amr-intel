"""
amr_sentinel/evidence/summary.py

Read-only evidence summary for the dashboard "Evidence" screen.

Answers, from the live database and with no estimates:
  * what data the platform holds, by source, with year ranges and last-ingest dates
  * what each ingestor run did (as logged), by source
  * how long the detection pipeline has been producing alerts, and how recently
  * how many signals have been validated against an official source, and from
    how many distinct sources (so the sample size is always visible)
  * per-country coverage, with Nigeria and Malawi called out explicitly
    (zero is reported as zero)

Inputs:  a SQLAlchemy Session
Outputs: a JSON-serialisable dict (see build_evidence_summary)
Dependencies: sqlalchemy, amr_sentinel.db.models

Design rules:
  * Aggregates only: no raw rows, no error messages, no metadata blobs.
  * Alert counts are reported by signal type only. Severity-tier counts are
    deliberately left out so this screen never headlines a number that triage
    changes (see Task 6, alert inflation) would alter.
  * Genomic signals (gene presence) are never merged with phenotypic
    resistance rates; they are reported in separate fields.
  * rows_skipped / rows_errored are reported exactly as each ingestor logged
    them. They are not relabelled as "plausibility rejects".
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, case, column, desc, func, inspect, null, select, table
from sqlalchemy.orm import Session

from amr_sentinel.db.models import (
    Alert,
    GenomicSignal,
    IngestionLog,
    ResistanceRecord,
    SignalValidation,
)

logger = logging.getLogger(__name__)

FOCUS_COUNTRIES = ("NGA", "MWI")
LOW_N_THRESHOLD = 30
RECENT_RUNS_LIMIT = 15
ACTIVITY_DAYS = 14
CACHE_TTL_SECONDS = 60
GENOMIC_SIGNAL_TYPE = "genomic_precursor"

_cache: Dict[str, Any] = {"at": 0.0, "data": None}


def _iso(dt: Any) -> Optional[str]:
    """Return an ISO-8601 UTC string, treating naive datetimes as UTC."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _utc(dt: datetime) -> datetime:
    """Return a timezone-aware UTC datetime, treating naive values as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def label_source(raw: str) -> str:
    """Map a data_source code to a readable label; fall back to the raw code."""
    up = (raw or "").upper()
    if "GLASS" in up:
        return "WHO GLASS"
    if "ECDC" in up or "EARS" in up:
        return "ECDC EARS-Net"
    if "PUBMED" in up or "LITERATURE" in up:
        return "PubMed (published literature)"
    return raw or "unknown"


def _sources(db: Session) -> List[Dict[str, Any]]:
    """Per-source record counts, coverage and last-ingest time."""
    rr = ResistanceRecord
    rows = (
        db.query(
            rr.data_source,
            func.count(rr.id),
            func.count(func.distinct(rr.country_iso3)),
            func.min(rr.year),
            func.max(rr.year),
            func.max(rr.ingested_at),
        )
        .group_by(rr.data_source)
        .order_by(func.count(rr.id).desc())
        .all()
    )
    out: List[Dict[str, Any]] = [
        {
            "kind": "phenotypic",
            "source": src,
            "label": label_source(src),
            "records": int(n),
            "countries": int(countries),
            "first_year": first,
            "last_year": last,
            "last_ingested_at": _iso(last_ing),
        }
        for src, n, countries, first, last, last_ing in rows
    ]
    gs = GenomicSignal
    g = db.query(
        func.count(gs.id),
        func.coalesce(func.sum(gs.isolate_count), 0),
        func.count(func.distinct(gs.country_iso3)),
        func.min(gs.year),
        func.max(gs.year),
        func.max(gs.last_updated),
    ).one()
    if g[0]:
        out.append({
            "kind": "genomic",
            "source": "NCBI_NDARO",
            "label": "NCBI NDARO (genomic)",
            "records": int(g[0]),
            "isolates": int(g[1]),
            "countries": int(g[2]),
            "first_year": g[3],
            "last_year": g[4],
            "last_ingested_at": _iso(g[5]),
        })
    return out


# The live ingestion_log table does not always match models.py, so this section
# reads the table's real columns and only uses the ones that exist. Missing
# counters are reported as null rather than guessed.
_SOURCE_ALIASES = ("source", "source_name", "ingestor", "name")
_STARTED_ALIASES = ("started_at", "start_time", "run_started_at", "created_at")
_COMPLETED_ALIASES = ("completed_at", "finished_at", "ended_at", "end_time")
_STATUS_ALIASES = ("status", "state")
_COUNTER_ALIASES = {
    "rows_fetched": ("rows_fetched", "records_fetched", "fetched", "rows_read"),
    "rows_inserted": ("rows_inserted", "records_inserted", "inserted", "rows_written", "rows_upserted"),
    "rows_skipped": ("rows_skipped", "records_skipped", "skipped", "rows_rejected", "records_rejected"),
    "rows_errored": ("rows_errored", "records_errored", "errors", "rows_failed", "error_count"),
}


def _pick(cols: set, aliases: tuple) -> Optional[str]:
    """Return the first alias present in cols, else None."""
    return next((a for a in aliases if a in cols), None)


def _int_or_none(v: Any) -> Optional[int]:
    """Convert a numeric DB value to int, keeping None as None."""
    return None if v is None else int(v)


def _ingestion(db: Session) -> Dict[str, Any]:
    """Ingestion audit: per-source totals, status counts and the latest runs."""
    cols = {c["name"] for c in inspect(db.get_bind()).get_columns("ingestion_log")}
    src = _pick(cols, _SOURCE_ALIASES)
    if src is None:
        raise RuntimeError("ingestion_log has no recognisable source column")
    started = _pick(cols, _STARTED_ALIASES)
    completed = _pick(cols, _COMPLETED_ALIASES)
    status = _pick(cols, _STATUS_ALIASES)
    counters = {key: _pick(cols, aliases) for key, aliases in _COUNTER_ALIASES.items()}
    logger.info("ingestion_log columns in use: source=%s started=%s completed=%s status=%s counters=%s",
                src, started, completed, status, counters)

    c = table("ingestion_log", *[column(n) for n in sorted(cols)]).c

    exprs = [c[src].label("source"), func.count().label("runs")]
    for key, col in counters.items():
        exprs.append((func.coalesce(func.sum(c[col]), 0) if col else null()).label(key))
    exprs.append((func.max(c[started]) if started else null()).label("last_started"))
    exprs.append((func.max(c[completed]) if completed else null()).label("last_completed"))
    sums = db.execute(select(*exprs).group_by(c[src]).order_by(c[src])).all()

    status_by_source: Dict[str, Dict[str, int]] = {}
    if status:
        for s_src, s_status, n in db.execute(
            select(c[src], c[status], func.count()).group_by(c[src], c[status])
        ).all():
            status_by_source.setdefault(s_src, {})[str(s_status)] = int(n)

    by_source = []
    for r in sums:
        m = r._mapping
        by_source.append({
            "source": m["source"],
            "label": label_source(m["source"]),
            "runs": int(m["runs"]),
            "rows_fetched": _int_or_none(m["rows_fetched"]),
            "rows_inserted": _int_or_none(m["rows_inserted"]),
            "rows_skipped": _int_or_none(m["rows_skipped"]),
            "rows_errored": _int_or_none(m["rows_errored"]),
            "last_started_at": _iso(m["last_started"]),
            "last_completed_at": _iso(m["last_completed"]),
            "status_counts": status_by_source.get(m["source"], {}),
        })

    recent_exprs = [c[src].label("source")]
    for label, col in (("started_at", started), ("completed_at", completed), ("status", status)):
        recent_exprs.append((c[col] if col else null()).label(label))
    for key, col in counters.items():
        recent_exprs.append((c[col] if col else null()).label(key))
    stmt = select(*recent_exprs)
    if started:
        stmt = stmt.order_by(desc(c[started]))
    recent_runs = []
    for r in db.execute(stmt.limit(RECENT_RUNS_LIMIT)).all():
        m = r._mapping
        recent_runs.append({
            "source": m["source"], "label": label_source(m["source"]),
            "started_at": _iso(m["started_at"]), "completed_at": _iso(m["completed_at"]),
            "status": m["status"],
            "rows_fetched": _int_or_none(m["rows_fetched"]), "rows_inserted": _int_or_none(m["rows_inserted"]),
            "rows_skipped": _int_or_none(m["rows_skipped"]), "rows_errored": _int_or_none(m["rows_errored"]),
        })
    return {"by_source": by_source, "recent_runs": recent_runs}


def _alerts(db: Session, now: datetime) -> Dict[str, Any]:
    """Alert totals by signal type, outcome counts and 14-day activity."""
    by_type = {
        t: int(n) for t, n in db.query(Alert.signal_type, func.count(Alert.id)).group_by(Alert.signal_type).all()
    }
    first, last, confirmed = db.query(
        func.min(Alert.created_at), func.max(Alert.created_at), func.max(Alert.updated_at)
    ).one()
    confirmed_true = db.query(func.count(Alert.id)).filter(Alert.outcome_confirmed.is_(True)).scalar() or 0
    confirmed_false = db.query(func.count(Alert.id)).filter(Alert.outcome_confirmed.is_(False)).scalar() or 0

    start_day = (now - timedelta(days=ACTIVITY_DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    buckets: Dict[str, Dict[str, int]] = {}
    for i in range(ACTIVITY_DAYS):
        day = (start_day + timedelta(days=i)).date().isoformat()
        buckets[day] = {"phenotypic": 0, "genomic": 0}
    for created, sig in db.query(Alert.created_at, Alert.signal_type).filter(Alert.created_at >= start_day).all():
        day = _utc(created).date().isoformat()
        if day in buckets:
            buckets[day]["genomic" if sig == GENOMIC_SIGNAL_TYPE else "phenotypic"] += 1
    return {
        "total": sum(by_type.values()),
        "by_signal_type": by_type,
        "outcome_confirmed": int(confirmed_true),
        "outcome_false_positive": int(confirmed_false),
        "first_alert_at": _iso(first),
        "last_alert_at": _iso(last),
        "last_confirmed_at": _iso(confirmed),
        "activity_14d": [{"date": d, **v} for d, v in buckets.items()],
    }


def _validations(db: Session) -> Dict[str, Any]:
    """Lead-time validation summary, always with the sample size and source count."""
    rows = (
        db.query(SignalValidation.lead_time_days, SignalValidation.official_recognition_source)
        .filter(SignalValidation.validation_status == "confirmed")
        .all()
    )
    if not rows:
        return {"n": 0, "sources": []}
    days = [r[0] for r in rows]
    avg = sum(days) / len(days)
    return {
        "n": len(days),
        "avg_days": int(avg),
        "avg_months": round(avg / 30.4, 1),
        "min_days": min(days),
        "max_days": max(days),
        "sources": sorted({r[1] for r in rows if r[1]}),
    }


def _coverage(db: Session) -> Dict[str, Any]:
    """Per-country coverage (phenotypic and genomic) plus the focus-country block."""
    rr = ResistanceRecord
    pheno = (
        db.query(
            rr.country_iso3,
            func.max(rr.region_who),
            func.count(rr.id),
            func.count(func.distinct(rr.pathogen_name)),
            func.min(rr.year),
            func.max(rr.year),
            func.sum(case((and_(rr.sample_count.isnot(None), rr.sample_count < LOW_N_THRESHOLD), 1), else_=0)),
            func.sum(case((rr.sample_count.is_(None), 1), else_=0)),
        )
        .group_by(rr.country_iso3)
        .all()
    )
    sources_rows = db.query(rr.country_iso3, rr.data_source).distinct().all()
    sources_by_country: Dict[str, List[str]] = {}
    for iso, src in sources_rows:
        sources_by_country.setdefault(iso, []).append(label_source(src))

    gs = GenomicSignal
    genomic = (
        db.query(
            gs.country_iso3,
            func.max(gs.region_who),
            func.count(gs.id),
            func.coalesce(func.sum(gs.isolate_count), 0),
            func.min(gs.year),
            func.max(gs.year),
        )
        .group_by(gs.country_iso3)
        .all()
    )

    countries: Dict[str, Dict[str, Any]] = {}

    def blank(iso: str) -> Dict[str, Any]:
        return {
            "country_iso3": iso, "region_who": None, "records": 0, "pathogens": 0,
            "first_year": None, "last_year": None, "sources": [],
            "records_low_n": 0, "records_no_n": 0,
            "genomic_rows": 0, "genomic_isolates": 0,
            "genomic_first_year": None, "genomic_last_year": None, "genomic_only": False,
        }

    for iso, region, n, pathogens, first, last, low_n, no_n in pheno:
        c = countries.setdefault(iso, blank(iso))
        c.update({
            "region_who": region, "records": int(n), "pathogens": int(pathogens),
            "first_year": first, "last_year": last,
            "sources": sorted(set(sources_by_country.get(iso, []))),
            "records_low_n": int(low_n or 0), "records_no_n": int(no_n or 0),
        })
    for iso, region, n, isolates, first, last in genomic:
        c = countries.setdefault(iso, blank(iso))
        c["region_who"] = c["region_who"] or region
        c.update({
            "genomic_rows": int(n), "genomic_isolates": int(isolates),
            "genomic_first_year": first, "genomic_last_year": last,
        })
    for c in countries.values():
        c["genomic_only"] = c["records"] == 0 and c["genomic_rows"] > 0

    rows = sorted(countries.values(), key=lambda c: (-c["records"], c["country_iso3"]))

    focus: List[Dict[str, Any]] = []
    for iso in FOCUS_COUNTRIES:
        row = dict(countries.get(iso) or blank(iso))
        row["alerts"] = int(db.query(func.count(Alert.id)).filter(Alert.country_iso3 == iso).scalar() or 0)
        row["has_phenotypic"] = row["records"] > 0
        row["has_genomic"] = row["genomic_rows"] > 0
        focus.append(row)
    return {"countries": rows, "focus": focus}


def _safe(db: Session, name: str, fn: Any, default: Any, failed: Dict[str, str]) -> Any:
    """
    Run one section; on any error log it, roll back the session and return the
    default so a single failing section cannot blank the whole screen.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - isolate each section
        logger.exception("evidence section '%s' failed", name)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        failed[name] = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]
        return default


def build_evidence_summary(
    db: Session,
    now: Optional[datetime] = None,
    collect_errors: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Build the evidence summary from the live database. Read-only.

    Each section (sources, ingestion, alerts, validations, coverage) runs
    independently. A failing section is listed in the "unavailable" key and its
    totals are null, never zero, so a failure cannot read as "no data".

    Args:
        db: Open SQLAlchemy session.
        now: Override for the current time (tests only).
        collect_errors: Optional dict that receives section -> first error line
            (for the command-line check; never returned by the API).

    Returns:
        Dict with keys: generated_at, totals, sources, ingestion, alerts,
        validations, coverage, unavailable, notes.
    """
    now = now or datetime.now(timezone.utc)
    failed: Dict[str, str] = {}
    sources = _safe(db, "sources", lambda: _sources(db), [], failed)
    ingestion = _safe(db, "ingestion", lambda: _ingestion(db), {"by_source": [], "recent_runs": []}, failed)
    alerts = _safe(db, "alerts", lambda: _alerts(db, now), {
        "total": None, "by_signal_type": {}, "outcome_confirmed": None, "outcome_false_positive": None,
        "first_alert_at": None, "last_alert_at": None, "last_confirmed_at": None, "activity_14d": [],
    }, failed)
    validations = _safe(db, "validations", lambda: _validations(db), {"n": 0, "sources": []}, failed)
    coverage = _safe(db, "coverage", lambda: _coverage(db), {"countries": [], "focus": []}, failed)

    rows = coverage["countries"]
    src_ok, cov_ok = "sources" not in failed, "coverage" not in failed
    totals = {
        "phenotypic_records": sum(s["records"] for s in sources if s["kind"] == "phenotypic") if src_ok else None,
        "genomic_signal_rows": sum(s["records"] for s in sources if s["kind"] == "genomic") if src_ok else None,
        "genomic_isolates": sum(s.get("isolates", 0) for s in sources if s["kind"] == "genomic") if src_ok else None,
        "countries_phenotypic": sum(1 for c in rows if c["records"] > 0) if cov_ok else None,
        "countries_genomic": sum(1 for c in rows if c["genomic_rows"] > 0) if cov_ok else None,
        "countries_genomic_only": sum(1 for c in rows if c["genomic_only"]) if cov_ok else None,
    }
    if collect_errors is not None:
        collect_errors.update(failed)
    return {
        "generated_at": _iso(now),
        "totals": totals,
        "sources": sources,
        "ingestion": ingestion,
        "alerts": alerts,
        "validations": validations,
        "coverage": coverage,
        "unavailable": sorted(failed),
        "notes": [
            "Counts are read live from the production database.",
            "Rows skipped and errored are reported exactly as each ingestor logged them.",
            "Genomic signals (gene presence) are never combined with phenotypic resistance rates.",
            "Severity-tier counts are intentionally not shown on this screen.",
        ],
    }


def get_evidence_summary(db: Session, ttl_seconds: int = CACHE_TTL_SECONDS) -> Dict[str, Any]:
    """Return the evidence summary, cached in-process for ttl_seconds."""
    now = time.monotonic()
    if ttl_seconds > 0 and _cache["data"] is not None and now - _cache["at"] < ttl_seconds:
        return _cache["data"]
    data = build_evidence_summary(db)
    _cache["data"], _cache["at"] = data, now
    logger.info("evidence summary rebuilt: %d phenotypic records, %d countries",
                data["totals"]["phenotypic_records"], len(data["coverage"]["countries"]))
    return data


if __name__ == "__main__":
    import json

    from amr_sentinel.db.database import SessionLocal

    logging.basicConfig(level=logging.WARNING)
    with SessionLocal() as session:
        errs: Dict[str, str] = {}
        result = build_evidence_summary(session, collect_errors=errs)
    print("SECTION STATUS")
    for name in ("sources", "ingestion", "alerts", "validations", "coverage"):
        print("  " + name + ": " + ("FAILED - " + errs[name] if name in errs else "OK"))
    print()
    print(json.dumps(result, indent=2, default=str))