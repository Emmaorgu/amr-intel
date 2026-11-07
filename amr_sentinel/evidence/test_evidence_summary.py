"""
tests/test_evidence_summary.py

Unit tests for amr_sentinel.evidence.summary using an in-memory SQLite database.

IMPORTANT: every row below is created inside the test run only to exercise the
aggregation logic. None of it is demo data and none of it may be written to a
real database. SQLite stands in for PostgreSQL, so JSONB and BigInteger get
SQLite-compatible compile hooks that live in this test file only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from amr_sentinel.db.models import (
    Alert, Base, GenomicSignal, IngestionLog, ResistanceRecord, SignalValidation,
)
from amr_sentinel.evidence import summary as ev


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


# models.py declares two indexes twice (ix_alerts_updated_at and
# ix_signal_validations_alert_id: index=True on the column plus an explicit Index
# in __table_args__). PostgreSQL schema comes from Alembic, so production is
# unaffected, but SQLite create_all() would fail. Drop the duplicates from the
# in-memory metadata for these tests only; models.py is not modified.
for _table in Base.metadata.tables.values():
    _seen: set = set()
    for _idx in list(_table.indexes):
        if _idx.name in _seen:
            _table.indexes.discard(_idx)
        _seen.add(_idx.name)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def rec(src: str, iso: str, year: int, n, path: str = "E. coli", abx: str = "Ceftriaxone") -> ResistanceRecord:
    return ResistanceRecord(pathogen_name=path, antibiotic_name=abx, country_iso3=iso, year=year,
                            resistance_rate=0.2, sample_count=n, data_source=src, region_who=None)


def alert(created: datetime, sig: str = "rate_spike", iso: str = "BGR", outcome=None) -> Alert:
    return Alert(id=uuid.uuid4(), created_at=created, updated_at=created, pathogen_name="E. coli",
                 antibiotic_name="Ceftriaxone", country_iso3=iso, severity_score=70, severity_tier="warn",
                 signal_type=sig, outcome_confirmed=outcome)


def test_empty_database_returns_zeros_not_errors(db: Session) -> None:
    s = ev.build_evidence_summary(db, now=NOW)
    assert s["totals"]["phenotypic_records"] == 0
    assert s["sources"] == [] and s["validations"] == {"n": 0, "sources": []}
    assert [f["country_iso3"] for f in s["coverage"]["focus"]] == ["NGA", "MWI"]
    assert all(f["has_phenotypic"] is False and f["records"] == 0 for f in s["coverage"]["focus"])
    assert len(s["alerts"]["activity_14d"]) == 14


def test_sources_group_and_label(db: Session) -> None:
    db.add_all([rec("GLASS", "KEN", 2019, 50), rec("GLASS", "KEN", 2020, 40),
                rec("ECDC", "BGR", 2021, 100), rec("PUBMED", "NGA", 2018, None)])
    db.add(GenomicSignal(gene_name="blaNDM", pathogen_name="E. coli", country_iso3="FRA", year=2022, isolate_count=7))
    db.commit()
    s = ev.build_evidence_summary(db, now=NOW)
    by = {x["source"]: x for x in s["sources"]}
    assert by["GLASS"]["records"] == 2 and by["GLASS"]["label"] == "WHO GLASS"
    assert (by["GLASS"]["first_year"], by["GLASS"]["last_year"]) == (2019, 2020)
    assert by["PUBMED"]["label"].startswith("PubMed")
    assert by["NCBI_NDARO"]["kind"] == "genomic" and by["NCBI_NDARO"]["isolates"] == 7
    assert s["totals"]["phenotypic_records"] == 4 and s["totals"]["genomic_signal_rows"] == 1


def test_coverage_focus_low_n_and_genomic_only(db: Session) -> None:
    db.add_all([rec("PUBMED", "NGA", 2018, None), rec("PUBMED", "NGA", 2019, 12), rec("GLASS", "NGA", 2020, 80)])
    db.add(GenomicSignal(gene_name="mcr-1", pathogen_name="E. coli", country_iso3="BGD", year=2022, isolate_count=3))
    db.add(alert(NOW, iso="NGA"))
    db.commit()
    cov = ev.build_evidence_summary(db, now=NOW)["coverage"]
    nga = next(f for f in cov["focus"] if f["country_iso3"] == "NGA")
    mwi = next(f for f in cov["focus"] if f["country_iso3"] == "MWI")
    assert nga["records"] == 3 and nga["records_no_n"] == 1 and nga["records_low_n"] == 1 and nga["alerts"] == 1
    assert nga["sources"] == ["PubMed (published literature)", "WHO GLASS"]
    assert mwi["records"] == 0 and mwi["has_phenotypic"] is False and mwi["has_genomic"] is False
    bgd = next(c for c in cov["countries"] if c["country_iso3"] == "BGD")
    assert bgd["genomic_only"] is True and bgd["records"] == 0


def test_ingestion_totals_status_counts_and_no_error_text(db: Session) -> None:
    t0 = NOW - timedelta(days=1)
    db.add_all([
        IngestionLog(source="GLASS", started_at=t0, completed_at=t0, status="success",
                     rows_fetched=10, rows_inserted=8, rows_skipped=2, rows_errored=0),
        IngestionLog(source="GLASS", started_at=NOW, completed_at=None, status="failed",
                     rows_fetched=5, rows_inserted=0, rows_skipped=0, rows_errored=5, error_message="secret detail"),
    ])
    db.commit()
    ing = ev.build_evidence_summary(db, now=NOW)["ingestion"]
    g = ing["by_source"][0]
    assert g["runs"] == 2 and g["rows_fetched"] == 15 and g["rows_errored"] == 5
    assert g["status_counts"] == {"success": 1, "failed": 1}
    assert ing["recent_runs"][0]["status"] == "failed"
    assert "secret detail" not in str(ing) and "error_message" not in str(ing)


def test_alert_counts_outcomes_and_activity_buckets(db: Session) -> None:
    db.add_all([
        alert(NOW, "rate_spike"), alert(NOW, "genomic_precursor"),
        alert(NOW - timedelta(days=3), "trajectory_deviation", outcome=True),
        alert(NOW - timedelta(days=40), "rate_spike", outcome=False),
    ])
    db.commit()
    a = ev.build_evidence_summary(db, now=NOW)["alerts"]
    assert a["total"] == 4 and a["by_signal_type"]["genomic_precursor"] == 1
    assert a["outcome_confirmed"] == 1 and a["outcome_false_positive"] == 1
    days = {d["date"]: d for d in a["activity_14d"]}
    assert days["2026-11-07"] == {"date": "2026-11-07", "phenotypic": 1, "genomic": 1}
    assert days["2026-11-04"]["phenotypic"] == 1
    assert sum(d["phenotypic"] + d["genomic"] for d in a["activity_14d"]) == 3
    assert "critical" not in str(a) and "severity" not in str(a)


def test_validation_summary_reports_n_and_distinct_sources(db: Session) -> None:
    a1, a2 = alert(NOW), alert(NOW)
    db.add_all([a1, a2])
    db.flush()
    for al, days in ((a1, 200), (a2, 300)):
        db.add(SignalValidation(alert_id=al.id, signal_detected_at=NOW, official_recognition_source="ECDC Annual Report",
                                official_recognition_date=NOW, lead_time_days=days, validation_status="confirmed"))
    db.commit()
    v = ev.build_evidence_summary(db, now=NOW)["validations"]
    assert v["n"] == 2 and v["avg_days"] == 250 and v["min_days"] == 200 and v["max_days"] == 300
    assert v["sources"] == ["ECDC Annual Report"] and v["avg_months"] == round(250 / 30.4, 1)


def test_cache_returns_same_object_until_ttl(db: Session) -> None:
    ev._cache.update({"at": 0.0, "data": None})
    first = ev.get_evidence_summary(db, ttl_seconds=60)
    db.add(rec("GLASS", "KEN", 2020, 10))
    db.commit()
    assert ev.get_evidence_summary(db, ttl_seconds=60) is first
    assert ev.get_evidence_summary(db, ttl_seconds=0)["totals"]["phenotypic_records"] == 1
