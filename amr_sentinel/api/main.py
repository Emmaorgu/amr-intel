"""
amr_sentinel/api/main.py
=========================
FastAPI backend for AMR-Sentinel.

Endpoints:
    GET  /health
    GET  /stats
    GET  /alerts
    GET  /alerts/{alert_id}
    PATCH /alerts/{alert_id}/feedback
    GET  /resistance-trends
    GET  /lead-times
    GET  /emergence-radar
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from amr_sentinel.db.database import SessionLocal, engine
from amr_sentinel.db.models import (
    Alert, EmergenceScore, Feedback, ResistanceRecord, SignalValidation
)

load_dotenv()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AMR-Sentinel Intelligence API",
    description="Autonomous pathogen intelligence. Early warning for antimicrobial resistance.",
    version="0.5.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS", "http://localhost:3000,http://localhost:5173"
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)

API_KEY = os.getenv("API_KEY", "")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_api_key(api_key: Optional[str] = Security(api_key_header)) -> str:
    if not API_KEY:
        return ""
    if api_key == API_KEY:
        return api_key
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                        detail="Invalid or missing API key.")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CitationSchema(BaseModel):
    pmid: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    year: Optional[int] = None
    journal: Optional[str] = None
    pubmed_url: Optional[str] = None
    class Config:
        from_attributes = True


class AlertSummarySchema(BaseModel):
    # Core fields (typed columns)
    alert_id: Optional[str] = None     # alias for id, used by frontend
    id: uuid.UUID
    created_at: datetime
    updated_at: Optional[datetime] = None   # last pipeline confirmation — time filters compare against this
    pathogen_name: str
    antibiotic_name: str
    antibiotic_class: Optional[str] = None
    country_iso3: str
    region_who: Optional[str] = None
    severity_score: int
    severity_tier: str
    signal_type: str
    current_resistance: Optional[float] = None
    forecasted_rate: Optional[float] = None
    deviation_magnitude: Optional[float] = None
    trend_direction: Optional[str] = None
    outcome_confirmed: Optional[bool] = None
    forecast_lower_80: Optional[float] = None
    forecast_upper_80: Optional[float] = None
    forecast_lower_50: Optional[float] = None
    forecast_upper_50: Optional[float] = None
    # Genomic precursor fields (unpacked from extra_data)
    gene_name: Optional[str] = None
    gene_family: Optional[str] = None
    gene_description: Optional[str] = None
    isolate_count: Optional[int] = None
    latest_year: Optional[int] = None
    time_series: Optional[dict] = None
    time_series_summary: Optional[str] = None
    acceleration_score: Optional[float] = None
    doubling_time_years: Optional[float] = None
    days_to_threshold: Optional[int] = None
    phenotypic_gap: Optional[str] = None
    surveillance_confidence: Optional[str] = None
    precursor_tier: Optional[str] = None
    surveillance_caveat: Optional[str] = None
    spread_risk_countries: Optional[list] = None
    intelligence_summary: Optional[str] = None
    who_priority: Optional[str] = None

    class Config:
        from_attributes = True


class AlertDetailSchema(AlertSummarySchema):
    stewardship_guidance: Optional[str] = None
    evidence_citations: Optional[list[CitationSchema]] = None
    pipeline_run_id: Optional[str] = None
    outcome_notes: Optional[str] = None
    outcome_recorded_at: Optional[datetime] = None


class FeedbackInputSchema(BaseModel):
    feedback_score: int = Field(..., ge=-1, le=1)
    feedback_type: str = Field(default="clinical", pattern="^(clinical|analyst|system)$")
    feedback_note: Optional[str] = Field(default=None, max_length=2000)
    action_taken: Optional[str] = Field(default=None, max_length=255)
    institution_id: Optional[str] = Field(default=None, max_length=100)


class FeedbackResponseSchema(BaseModel):
    id: int
    alert_id: uuid.UUID
    feedback_score: int
    feedback_type: str
    submitted_at: datetime
    class Config:
        from_attributes = True


class StatsSchema(BaseModel):
    total_alerts: int
    critical_alerts: int          # phenotypic only — excludes genomic precursors
    critical_alerts_today: int = 0
    new_alerts_today: int = 0
    warn_alerts: int              # phenotypic only
    genomic_precursor_alerts: int = 0   # total genomic precursor count (separate signal class)
    validated_alerts: int
    false_positive_alerts: int
    pathogens_monitored: int
    countries_monitored: int
    last_pipeline_run: Optional[datetime] = None
    resistance_records_total: int
    genomic_signals_total: int = 0
    avg_lead_time_days: Optional[int] = None
    avg_lead_time_months: Optional[float] = None
    validated_signals_count: int = 0
    emerging_threats_count: int = 0


class TrendPointSchema(BaseModel):
    year: int
    resistance_rate: float
    sample_count: Optional[int] = None
    data_source: str


class TrendSeriesSchema(BaseModel):
    pathogen_name: str
    antibiotic_name: str
    country_iso3: str
    data_points: list[TrendPointSchema]


class AlertListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    alerts: list[AlertSummarySchema]


class LeadTimeSchema(BaseModel):
    id: int
    alert_id: uuid.UUID
    pathogen_name: str
    antibiotic_name: str
    country_iso3: str
    severity_score: int
    current_resistance: Optional[float] = None
    validated_resistance_rate: Optional[float] = None
    signal_detected_at: datetime
    official_recognition_source: str
    official_recognition_date: datetime
    official_recognition_url: Optional[str] = None
    lead_time_days: int
    lead_time_months: float
    validation_status: str
    notes: Optional[str] = None
    class Config:
        from_attributes = True


class EmergenceRadarSchema(BaseModel):
    id: int
    pathogen_name: str
    antibiotic_name: str
    country_iso3: str
    region_who: Optional[str] = None
    emergence_score: int
    emergence_tier: str
    acceleration_score: int
    new_appearance_score: int
    spread_velocity_score: int
    endemic_penalty: int
    acceleration_rate: Optional[float] = None
    acceleration_index: Optional[int] = None
    baseline_rate: Optional[float] = None
    current_rate: Optional[float] = None
    years_observed: Optional[int] = None
    data_confidence: Optional[str] = None
    has_baseline: Optional[bool] = None
    computed_at: datetime
    # Additional fields used by dashboard
    why_emerging: Optional[str] = None
    driver_phrase: Optional[str] = None
    current_resistance: Optional[float] = None
    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_citations(raw) -> list[CitationSchema]:
    """Parse evidence_citations from DB (may be JSON string or list)."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [
        CitationSchema(**{k: v for k, v in item.items()
                         if k in CitationSchema.model_fields})
        for item in raw if isinstance(item, dict)
    ]


def _alert_to_dict(alert: Alert) -> dict[str, Any]:
    """
    Serialize an Alert ORM object to a dict, merging extra_data fields
    for genomic precursor alerts.

    This is the single source of truth for alert serialization. Both the
    summary and detail endpoints use this to ensure genomic fields are
    always present when available.
    """
    # Start with core typed columns
    d: dict[str, Any] = {
        "alert_id": str(alert.id),  # frontend uses alert_id
        "id": alert.id,
        "created_at": alert.created_at,
        "updated_at": getattr(alert, "updated_at", None),
        "pathogen_name": alert.pathogen_name,
        "antibiotic_name": alert.antibiotic_name,
        "antibiotic_class": alert.antibiotic_class,
        "country_iso3": alert.country_iso3,
        "region_who": alert.region_who,
        "severity_score": alert.severity_score,
        "severity_tier": alert.severity_tier,
        "signal_type": alert.signal_type,
        "current_resistance": alert.current_resistance,
        "forecasted_rate": alert.forecasted_rate,
        "deviation_magnitude": alert.deviation_magnitude,
        "trend_direction": alert.trend_direction,
        "outcome_confirmed": alert.outcome_confirmed,
        "forecast_lower_80": alert.forecast_lower_80,
        "forecast_upper_80": alert.forecast_upper_80,
        "forecast_lower_50": alert.forecast_lower_50,
        "forecast_upper_50": alert.forecast_upper_50,
        # Detail fields
        "stewardship_guidance": getattr(alert, "stewardship_guidance", None),
        "evidence_citations": _parse_citations(getattr(alert, "evidence_citations", None)),
        "pipeline_run_id": getattr(alert, "pipeline_run_id", None),
        "outcome_notes": getattr(alert, "outcome_notes", None),
        "outcome_recorded_at": getattr(alert, "outcome_recorded_at", None),
    }

    # Unpack extra_data for genomic precursor alerts
    extra = alert.extra_data or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}

    genomic_fields = [
        "gene_name", "gene_family", "gene_description",
        "isolate_count", "latest_year", "time_series", "time_series_summary",
        "acceleration_score", "doubling_time_years", "days_to_threshold",
        "phenotypic_gap", "surveillance_confidence", "surveillance_caveat", "precursor_tier",
        "spread_risk_countries", "intelligence_summary", "who_priority",
    ]
    for gfield in genomic_fields:
        d[gfield] = extra.get(gfield)

    return d


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
def health_check():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected",
                "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Database unreachable: " + str(exc))


@app.get("/stats", response_model=StatsSchema, tags=["Intelligence"])
def get_stats(db: Session = Depends(get_db), _: str = Depends(get_api_key)):
    """
    Platform statistics.

    critical_alerts and warn_alerts count PHENOTYPIC signals only
    (trajectory_deviation and rate_spike). Genomic precursor alerts are a
    distinct signal class and reported separately as genomic_precursor_alerts.

    This separation prevents genomic precursor counts (which can be large and
    grow with each NDARO update) from inflating the headline operational
    metrics that clinicians and analysts use to triage their day.
    """
    # Total across all signal types
    total = db.query(Alert).count()

    # Phenotypic-only base query — excludes genomic_precursor
    phenotypic_q = db.query(Alert).filter(Alert.signal_type != "genomic_precursor")

    # Headline critical/warn counts: phenotypic only
    critical = phenotypic_q.filter(Alert.severity_tier == "critical").count()
    warn = phenotypic_q.filter(Alert.severity_tier == "warn").count()

    # Genomic precursor count — reported separately, not in critical headline
    genomic_precursor_count = db.query(Alert).filter(
        Alert.signal_type == "genomic_precursor"
    ).count()

    validated = db.query(Alert).filter(Alert.outcome_confirmed == True).count()
    false_pos = db.query(Alert).filter(Alert.outcome_confirmed == False).count()
    pathogens = db.query(Alert.pathogen_name).distinct().count()
    # Count from resistance_records — full source data coverage,
    # not alerts (which are a filtered high-signal subset).
    countries = db.query(ResistanceRecord.country_iso3).distinct().count()
    last_alert = db.query(Alert.created_at).order_by(Alert.created_at.desc()).first()
    last_run = last_alert[0] if last_alert else None
    rr_total = db.query(ResistanceRecord).count()

    # Rolling 24h window — phenotypic only for operational relevance
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    critical_today = (
        phenotypic_q
        .filter(Alert.severity_tier == "critical")
        .filter(Alert.created_at >= cutoff_24h)
        .count()
    )
    new_today = (
        phenotypic_q
        .filter(Alert.created_at >= cutoff_24h)
        .count()
    )

    # Genomic signals (raw isolate records, not alerts)
    try:
        from amr_sentinel.db.models import GenomicSignal
        genomic_total = db.query(GenomicSignal).count()
    except Exception:
        genomic_total = 0

    lead_times = db.query(SignalValidation.lead_time_days).filter(
        SignalValidation.validation_status == "confirmed"
    ).all()
    avg_days = None
    avg_months = None
    if lead_times:
        avg_days = int(sum(r[0] for r in lead_times) / len(lead_times))
        avg_months = round(avg_days / 30.4, 1)

    emerging_count = db.query(EmergenceScore).filter(
        EmergenceScore.emergence_tier == "emerging"
    ).count()

    return StatsSchema(
        total_alerts=total,
        critical_alerts=critical,
        critical_alerts_today=critical_today,
        new_alerts_today=new_today,
        warn_alerts=warn,
        genomic_precursor_alerts=genomic_precursor_count,
        validated_alerts=validated,
        false_positive_alerts=false_pos,
        pathogens_monitored=pathogens,
        countries_monitored=countries,
        last_pipeline_run=last_run,
        resistance_records_total=rr_total,
        genomic_signals_total=genomic_total,
        avg_lead_time_days=avg_days,
        avg_lead_time_months=avg_months,
        validated_signals_count=len(lead_times),
        emerging_threats_count=emerging_count,
    )


@app.get("/alerts", tags=["Intelligence"])
def list_alerts(
    tier: Optional[str] = Query(default=None),
    country: Optional[str] = Query(default=None),
    pathogen: Optional[str] = Query(default=None),
    antibiotic: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
    trend: Optional[str] = Query(default=None),
    signal_type: Optional[str] = Query(default=None),
    validated: Optional[bool] = Query(default=None),
    min_score: int = Query(default=0, ge=0, le=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=500),
    sort_by: str = Query(default="severity_score"),
    db: Session = Depends(get_db),
    _: str = Depends(get_api_key),
):
    q = db.query(Alert)
    if tier:
        q = q.filter(Alert.severity_tier == tier.lower())
    if country:
        q = q.filter(Alert.country_iso3 == country.upper())
    if pathogen:
        q = q.filter(Alert.pathogen_name.ilike("%" + pathogen + "%"))
    if antibiotic:
        q = q.filter(Alert.antibiotic_name.ilike("%" + antibiotic + "%"))
    if region:
        q = q.filter(Alert.region_who == region.upper())
    if trend:
        q = q.filter(Alert.trend_direction == trend.lower())
    if signal_type:
        q = q.filter(Alert.signal_type == signal_type)
    if validated is not None:
        q = q.filter(Alert.outcome_confirmed == validated)
    if min_score > 0:
        q = q.filter(Alert.severity_score >= min_score)

    total = q.count()
    q = q.order_by(
        Alert.severity_score.desc() if sort_by != "created_at"
        else Alert.created_at.desc()
    )
    offset = (page - 1) * page_size
    alerts = q.offset(offset).limit(page_size).all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "alerts": [_alert_to_dict(a) for a in alerts],
    }


@app.get("/alerts/{alert_id}", tags=["Intelligence"])
def get_alert(alert_id: uuid.UUID, db: Session = Depends(get_db),
              _: str = Depends(get_api_key)):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert " + str(alert_id) + " not found.")
    return _alert_to_dict(alert)


@app.patch("/alerts/{alert_id}/feedback", response_model=FeedbackResponseSchema,
           tags=["Intelligence"])
def submit_feedback(
    alert_id: uuid.UUID, payload: FeedbackInputSchema,
    db: Session = Depends(get_db), _: str = Depends(get_api_key),
):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert " + str(alert_id) + " not found.")

    feedback = Feedback(
        alert_id=alert_id, feedback_type=payload.feedback_type,
        feedback_score=payload.feedback_score, feedback_note=payload.feedback_note,
        action_taken=payload.action_taken, institution_id=payload.institution_id,
    )
    db.add(feedback)

    if payload.feedback_score == 1:
        alert.outcome_confirmed = True
        alert.outcome_recorded_at = datetime.now(timezone.utc)
    elif payload.feedback_score == -1:
        alert.outcome_confirmed = False
        alert.outcome_recorded_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(feedback)
    return FeedbackResponseSchema.model_validate(feedback)


@app.get("/resistance-trends", response_model=list[TrendSeriesSchema], tags=["Intelligence"])
def get_resistance_trends(
    pathogen: str = Query(...),
    antibiotic: str = Query(...),
    country: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    _: str = Depends(get_api_key),
):
    q = db.query(ResistanceRecord).filter(
        ResistanceRecord.pathogen_name.ilike("%" + pathogen + "%"),
        ResistanceRecord.antibiotic_name.ilike("%" + antibiotic + "%"),
    )
    if country:
        q = q.filter(ResistanceRecord.country_iso3 == country.upper())
    if source:
        q = q.filter(ResistanceRecord.data_source == source.upper())

    records = q.order_by(ResistanceRecord.country_iso3, ResistanceRecord.year).all()
    if not records:
        return []

    from collections import defaultdict
    series: dict[str, list] = defaultdict(list)
    for r in records:
        series[r.country_iso3].append(
            TrendPointSchema(year=r.year, resistance_rate=round(r.resistance_rate, 4),
                             sample_count=r.sample_count, data_source=r.data_source)
        )
    return [
        TrendSeriesSchema(
            pathogen_name=pathogen, antibiotic_name=antibiotic, country_iso3=iso3,
            data_points=sorted(points, key=lambda p: p.year),
        )
        for iso3, points in sorted(series.items())
    ]


@app.get("/lead-times", response_model=list[LeadTimeSchema], tags=["Intelligence"])
def get_lead_times(
    status_filter: Optional[str] = Query(default="confirmed", alias="status"),
    db: Session = Depends(get_db),
    _: str = Depends(get_api_key),
):
    q = (db.query(SignalValidation, Alert)
         .join(Alert, SignalValidation.alert_id == Alert.id))
    if status_filter and status_filter != "all":
        q = q.filter(SignalValidation.validation_status == status_filter)
    q = q.order_by(SignalValidation.lead_time_days.desc())

    return [
        LeadTimeSchema(
            id=v.id, alert_id=a.id,
            pathogen_name=a.pathogen_name, antibiotic_name=a.antibiotic_name,
            country_iso3=a.country_iso3, severity_score=a.severity_score,
            current_resistance=a.current_resistance,
            validated_resistance_rate=v.validated_resistance_rate,
            signal_detected_at=v.signal_detected_at,
            official_recognition_source=v.official_recognition_source,
            official_recognition_date=v.official_recognition_date,
            official_recognition_url=v.official_recognition_url,
            lead_time_days=v.lead_time_days,
            lead_time_months=round(v.lead_time_days / 30.4, 1),
            validation_status=v.validation_status, notes=v.notes,
        )
        for v, a in q.all()
    ]


@app.get("/emergence-radar", tags=["Intelligence"])
def get_emergence_radar(
    tier: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
    pathogen: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: str = Depends(get_api_key),
):
    """
    Resistance Emergence Radar — countries ranked by probability of becoming
    critical within 12 months.
    """
    q = db.query(EmergenceScore)

    if tier == "all":
        pass
    elif tier:
        q = q.filter(EmergenceScore.emergence_tier == tier)
    else:
        q = q.filter(EmergenceScore.emergence_tier.in_(["emerging", "escalating", "watch"]))

    if region:
        q = q.filter(EmergenceScore.region_who == region.upper())
    if pathogen:
        q = q.filter(EmergenceScore.pathogen_name.ilike("%" + pathogen + "%"))

    q = q.order_by(EmergenceScore.emergence_score.desc())
    scores = q.limit(limit).all()

    if not scores:
        return []

    result = []
    for s in scores:
        d = {
            "id": s.id,
            "pathogen_name": s.pathogen_name,
            "antibiotic_name": s.antibiotic_name,
            "country_iso3": s.country_iso3,
            "region_who": getattr(s, "region_who", None),
            "emergence_score": s.emergence_score,
            "emergence_tier": s.emergence_tier,
            "acceleration_score": s.acceleration_score,
            "new_appearance_score": s.new_appearance_score,
            "spread_velocity_score": s.spread_velocity_score,
            "endemic_penalty": s.endemic_penalty,
            "acceleration_rate": getattr(s, "acceleration_rate", None),
            "acceleration_index": getattr(s, "acceleration_index", None),
            "baseline_rate": getattr(s, "baseline_rate", None),
            "current_rate": getattr(s, "current_rate", None),
            "current_resistance": getattr(s, "current_rate", None),
            "years_observed": getattr(s, "years_observed", None),
            "data_confidence": getattr(s, "data_confidence", None),
            "has_baseline": getattr(s, "has_baseline", None),
            "computed_at": s.computed_at,
            "why_emerging": getattr(s, "why_emerging", None),
            "driver_phrase": getattr(s, "driver_phrase", None),
        }
        result.append(d)
    return result

@app.get("/alerts/{alert_id}/causal-context", tags=["Intelligence"])
def get_causal_context(
    alert_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: str = Depends(get_api_key),
):
    """
    On-demand causal intelligence for a phenotypic alert.

    Cross-references genomic_signals against resistance_records to generate
    a mechanistic explanation for the observed resistance trend. Returns
    gene-level evidence, doubling time, confidence, and narrative.
    """
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert " + str(alert_id) + " not found.")

    if alert.signal_type == "genomic_precursor":
        return {
            "alert_id": str(alert_id),
            "signal_type": "genomic_precursor",
            "message": "Causal context not applicable to genomic precursor alerts — "
                       "this signal IS the genomic layer.",
            "causal_confidence": "N/A",
        }

    try:
        from amr_sentinel.models.causal_intelligence import analyse_single_alert_from_db
        ctx = analyse_single_alert_from_db(
            alert_id=str(alert_id),
            pathogen_name=alert.pathogen_name,
            antibiotic_name=alert.antibiotic_name,
            country_iso3=alert.country_iso3,
            current_resistance=alert.current_resistance or 0.0,
            trend_direction=alert.trend_direction or "rising",
        )
        return {
            "alert_id": str(alert_id),
            "pathogen_name": ctx.pathogen_name,
            "antibiotic_name": ctx.antibiotic_name,
            "country_iso3": ctx.country_iso3,
            "phenotypic_rate": ctx.phenotypic_rate,
            "trend_direction": ctx.trend_direction,
            "causal_confidence": ctx.causal_confidence,
            "causal_narrative": ctx.causal_narrative,
            "lead_time_note": ctx.lead_time_note,
            "mechanism_summary": ctx.mechanism_summary,
            "genomic_genes": [
                {
                    "gene_name": g.gene_name,
                    "gene_family": g.gene_family,
                    "isolate_count": g.isolate_count,
                    "latest_year": g.latest_year,
                    "doubling_time_years": g.doubling_time_years,
                    "precursor_tier": g.precursor_tier,
                    "time_series": g.time_series,
                }
                for g in ctx.genomic_genes
            ],
            "analysed_at": ctx.analysed_at,
        }
    except Exception as exc:
        logger.error("Causal analysis failed for %s: %s", alert_id, exc)
        raise HTTPException(status_code=500, detail="Causal analysis failed: " + str(exc))

@app.get("/alerts/{alert_id}/history", tags=["Intelligence"])
def get_alert_history(
    alert_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: str = Depends(get_api_key),
):
    """
    State transition history for a resistance triplet.

    Returns the year-by-year tier progression (STABLE → WATCH → EMERGING →
    CRITICAL → IMPROVING) for the pathogen/antibiotic/country combination
    of the given alert. Populated by state_transition_tracker.py.
    """
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert " + str(alert_id) + " not found.")

    try:
        from amr_sentinel.models.state_transition_tracker import get_triplet_history
        history = get_triplet_history(
            db,
            pathogen_name=alert.pathogen_name,
            antibiotic_name=alert.antibiotic_name,
            country_iso3=alert.country_iso3,
        )
        return {
            "alert_id": str(alert_id),
            "pathogen_name": alert.pathogen_name,
            "antibiotic_name": alert.antibiotic_name,
            "country_iso3": alert.country_iso3,
            "history": history,
            "total_years": len(history),
            "tier_changes": sum(1 for h in history if h["tier_changed"]),
            "first_year": history[0]["year"] if history else None,
            "latest_tier": history[-1]["tier"] if history else None,
        }
    except Exception as exc:
        logger.error("History query failed for %s: %s", alert_id, exc)
        raise HTTPException(status_code=500, detail="History query failed: " + str(exc))


@app.get("/state-transitions", tags=["Intelligence"])
def get_state_transitions(
    pathogen: Optional[str] = Query(default=None),
    antibiotic: Optional[str] = Query(default=None),
    country: Optional[str] = Query(default=None),
    tier: Optional[str] = Query(default=None),
    tier_changed_only: bool = Query(default=False),
    year_from: Optional[int] = Query(default=None),
    year_to: Optional[int] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: str = Depends(get_api_key),
):
    """
    Query state transition history across all triplets.

    Useful for finding:
    - All EMERGING transitions in a given year
    - Which triplets moved fastest from STABLE to CRITICAL
    - Countries showing IMPROVING transitions
    """
    try:
        from amr_sentinel.db.models import StateTransition
    except ImportError:
        raise HTTPException(status_code=503, detail="State transitions table not yet created. Run alembic upgrade head.")

    q = db.query(StateTransition)
    if pathogen:
        q = q.filter(StateTransition.pathogen_name.ilike("%" + pathogen + "%"))
    if antibiotic:
        q = q.filter(StateTransition.antibiotic_name.ilike("%" + antibiotic + "%"))
    if country:
        q = q.filter(StateTransition.country_iso3 == country.upper())
    if tier:
        q = q.filter(StateTransition.tier == tier.upper())
    if tier_changed_only:
        q = q.filter(StateTransition.tier_changed == True)
    if year_from:
        q = q.filter(StateTransition.year >= year_from)
    if year_to:
        q = q.filter(StateTransition.year <= year_to)

    q = q.order_by(StateTransition.year.desc())
    rows = q.limit(limit).all()

    return [
        {
            "pathogen_name": r.pathogen_name,
            "antibiotic_name": r.antibiotic_name,
            "country_iso3": r.country_iso3,
            "year": r.year,
            "tier": r.tier,
            "previous_tier": r.previous_tier,
            "tier_changed": r.tier_changed,
            "resistance_rate": r.resistance_rate,
            "rate_change_1yr": r.rate_change_1yr,
            "years_in_previous_tier": r.years_in_previous_tier,
        }
        for r in rows
    ]