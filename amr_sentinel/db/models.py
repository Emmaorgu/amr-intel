"""
amr_sentinel/db/models.py
==========================
SQLAlchemy ORM Models.

Tables:
    resistance_records  — normalised resistance measurements
    ingestion_log       — audit log of every ingestor run
    alerts              — triaged intelligence alerts (phenotypic + genomic)
    feedback            — clinician feedback on alerts
    signal_validations  — lead-time tracking (Task 5.1)
    emergence_scores    — resistance emergence radar (Task 5.2)
    genomic_signals     — NCBI NDARO genomic signal aggregates (Task 5.8)

Dependencies:
    sqlalchemy, psycopg2-binary
"""

from __future__ import annotations

import uuid
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey,
    Index, Integer, JSON, SmallInteger, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class ResistanceRecord(Base):
    __tablename__ = "resistance_records"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pathogen_name = Column(String(255), nullable=False, index=True)
    pathogen_ncbi_id = Column(String(50), nullable=True)
    antibiotic_name = Column(String(255), nullable=False, index=True)
    antibiotic_class = Column(String(255), nullable=True)
    country_iso3 = Column(String(3), nullable=False, index=True)
    region_who = Column(String(50), nullable=True)
    year = Column(SmallInteger, nullable=False, index=True)
    quarter = Column(SmallInteger, nullable=True)
    resistance_rate = Column(Float, nullable=False)
    sample_count = Column(Integer, nullable=True)
    data_source = Column(String(50), nullable=False, index=True)
    source_record_id = Column(String(255), nullable=True)
    ingested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("pathogen_name", "antibiotic_name", "country_iso3",
                         "year", "quarter", "data_source", name="uq_resistance_record"),
        Index("ix_resistance_triplet", "pathogen_name", "antibiotic_name", "country_iso3"),
        Index("ix_resistance_source_year", "data_source", "year"),
    )


class IngestionLog(Base):
    __tablename__ = "ingestion_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source = Column(String(50), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(20), nullable=False)
    rows_fetched = Column(Integer, nullable=True)
    rows_inserted = Column(Integer, nullable=True)
    rows_skipped = Column(Integer, nullable=True)
    rows_errored = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    metadata_ = Column("metadata", JSON, nullable=True)

    __table_args__ = (
        Index("ix_ingestion_log_source_started", "source", "started_at"),
    )


class Alert(Base):
    """
    Triaged intelligence alert — the primary intelligence asset.

    Supports both phenotypic (trajectory_deviation, rate_spike) and
    genomic precursor (genomic_precursor) signal types.

    Genomic precursor alerts store their extended fields (gene_name,
    isolate_count, doubling_time_years, time_series, etc.) in the
    extra_data JSONB column, which the API unpacks on read.
    """
    __tablename__ = "alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    pipeline_run_id = Column(String(100), nullable=True, index=True)
    pathogen_name = Column(String(255), nullable=False, index=True)
    antibiotic_name = Column(String(255), nullable=False, index=True)
    antibiotic_class = Column(String(255), nullable=True)
    country_iso3 = Column(String(3), nullable=False, index=True)
    region_who = Column(String(50), nullable=True)
    severity_score = Column(SmallInteger, nullable=False, index=True)
    severity_tier = Column(String(20), nullable=False, index=True)
    signal_type = Column(String(50), nullable=False)
    current_resistance = Column(Float, nullable=True)
    forecasted_rate = Column(Float, nullable=True)
    deviation_magnitude = Column(Float, nullable=True)
    trend_direction = Column(String(20), nullable=True)
    data_year = Column(SmallInteger, nullable=True)
    stewardship_guidance = Column(Text, nullable=True)
    evidence_citations = Column(JSON, nullable=True)
    routing_target = Column(String(100), nullable=True)
    # Forecast confidence intervals (Task 5.5)
    forecast_lower_80 = Column(Float, nullable=True)
    forecast_upper_80 = Column(Float, nullable=True)
    forecast_lower_50 = Column(Float, nullable=True)
    forecast_upper_50 = Column(Float, nullable=True)
    outcome_confirmed = Column(Boolean, nullable=True)
    outcome_notes = Column(Text, nullable=True)
    outcome_recorded_at = Column(DateTime(timezone=True), nullable=True)
    # Extended fields for genomic precursor alerts and future signal types.
    # Stores: gene_name, gene_family, isolate_count, doubling_time_years,
    # days_to_threshold, time_series, surveillance_confidence, etc.
    extra_data = Column(JSONB, nullable=True)

    feedback = relationship("Feedback", back_populates="alert",
                            cascade="all, delete-orphan", lazy="select")
    validations = relationship("SignalValidation", back_populates="alert",
                               cascade="all, delete-orphan", lazy="select")

    __table_args__ = (
        Index("ix_alerts_triplet", "pathogen_name", "antibiotic_name", "country_iso3"),
        Index("ix_alerts_severity_created", "severity_tier", "created_at"),
        Index("ix_alerts_country_created", "country_iso3", "created_at"),
        Index("ix_alerts_signal_type", "signal_type"),
    )


class Feedback(Base):
    """Clinician or analyst feedback — closes the intelligence loop."""
    __tablename__ = "feedback"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    alert_id = Column(UUID(as_uuid=True), ForeignKey("alerts.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    feedback_type = Column(String(20), nullable=False, default="clinical")
    institution_id = Column(String(100), nullable=True)
    user_token = Column(String(100), nullable=True)
    feedback_score = Column(SmallInteger, nullable=False)
    feedback_note = Column(Text, nullable=True)
    action_taken = Column(String(255), nullable=True)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now(),
                          nullable=False, index=True)

    alert = relationship("Alert", back_populates="feedback")

    __table_args__ = (
        Index("ix_feedback_alert_submitted", "alert_id", "submitted_at"),
        Index("ix_feedback_score", "feedback_score"),
    )


class SignalValidation(Base):
    """
    Lead-time tracking — when AMR-Sentinel detected vs official recognition.
    The headline Abuja demo metric.
    """
    __tablename__ = "signal_validations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    alert_id = Column(UUID(as_uuid=True), ForeignKey("alerts.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    signal_detected_at = Column(DateTime(timezone=True), nullable=False)
    official_recognition_source = Column(String(500), nullable=False)
    official_recognition_date = Column(DateTime(timezone=True), nullable=False)
    official_recognition_url = Column(String(1000), nullable=True)
    lead_time_days = Column(Integer, nullable=False)
    validation_status = Column(String(20), nullable=False, default="confirmed")
    validated_resistance_rate = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    validated_by = Column(String(100), nullable=True)
    validated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    alert = relationship("Alert", back_populates="validations")

    __table_args__ = (
        Index("ix_signal_validations_alert_id", "alert_id"),
        Index("ix_signal_validations_lead_time", "lead_time_days"),
        Index("ix_signal_validations_status", "validation_status"),
    )


class EmergenceScore(Base):
    """
    Computed emergence score for a resistance triplet.
    Answers: "Which country is BECOMING the next Bulgaria?"
    """
    __tablename__ = "emergence_scores"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pathogen_name = Column(String(255), nullable=False)
    antibiotic_name = Column(String(255), nullable=False)
    country_iso3 = Column(String(3), nullable=False)
    region_who = Column(String(50), nullable=True)
    emergence_score = Column(SmallInteger, nullable=False)
    emergence_tier = Column(String(20), nullable=False)
    acceleration_score = Column(SmallInteger, nullable=False, default=0)
    new_appearance_score = Column(SmallInteger, nullable=False, default=0)
    spread_velocity_score = Column(SmallInteger, nullable=False, default=0)
    endemic_penalty = Column(SmallInteger, nullable=False, default=0)
    acceleration_rate = Column(Float, nullable=True)
    acceleration_index = Column(SmallInteger, nullable=True)
    baseline_rate = Column(Float, nullable=True)
    current_rate = Column(Float, nullable=True)
    years_observed = Column(SmallInteger, nullable=True)
    data_confidence = Column(String(10), nullable=True)
    has_baseline = Column(Boolean, nullable=True)
    computed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_emergence_scores_score", "emergence_score"),
        Index("ix_emergence_scores_tier", "emergence_tier"),
        Index("ix_emergence_scores_country", "country_iso3"),
        Index("ix_emergence_scores_confidence", "data_confidence"),
        Index("ix_emergence_scores_triplet",
              "pathogen_name", "antibiotic_name", "country_iso3"),
    )

    def __repr__(self) -> str:
        return (
            f"<EmergenceScore {self.pathogen_name}/{self.antibiotic_name}/"
            f"{self.country_iso3} score={self.emergence_score} [{self.emergence_tier}]>"
        )


class GenomicSignal(Base):
    """
    Aggregated genomic signal from NCBI NDARO.
    One row per (gene_name, pathogen_name, country_iso3, year).
    Isolate count = number of clinical isolates carrying the gene that year.
    """
    __tablename__ = "genomic_signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    gene_name = Column(String(100), nullable=False)
    gene_family = Column(String(50), nullable=True)
    drug_class = Column(String(100), nullable=True)
    pathogen_name = Column(String(255), nullable=False)
    country_iso3 = Column(String(3), nullable=False)
    region_who = Column(String(50), nullable=True)
    year = Column(SmallInteger, nullable=False)
    isolate_count = Column(Integer, nullable=False)
    last_updated = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("gene_name", "pathogen_name", "country_iso3", "year",
                         name="uq_genomic_signal"),
        Index("ix_genomic_signals_gene", "gene_name"),
        Index("ix_genomic_signals_pathogen", "pathogen_name"),
        Index("ix_genomic_signals_country", "country_iso3"),
        Index("ix_genomic_signals_year", "year"),
        Index("ix_genomic_signals_family", "gene_family"),
    )

    def __repr__(self) -> str:
        return (
            f"<GenomicSignal {self.gene_name}/{self.pathogen_name}/"
            f"{self.country_iso3} year={self.year} n={self.isolate_count}>"
        )

class StateTransition(Base):
    """
    State Transition Tracker — append-mode history of resistance tier
    per triplet per year.

    One row per (pathogen, antibiotic, country, year). Records the
    resistance tier at that point in time:
        STABLE      — resistance low, no acceleration
        WATCH       — resistance low but accelerating
        EMERGING    — new or rapidly rising resistance
        CRITICAL    — resistance high (>25%) or above threshold
        IMPROVING   — resistance previously high, now declining

    Answers:
        "Which threats moved from WATCH to EMERGING fastest?"
        "Which countries show IMPROVING transitions after intervention?"
        "How long did Croatia take to go from WATCH to CRITICAL?"

    Populated by:
        python -m amr_sentinel.models.state_transition_tracker

    Used by:
        GET /alerts/{id}/history — timeline for Alert Investigation History tab
        GET /state-transitions?pathogen=...&country=... — full history query
    """
    __tablename__ = "state_transitions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pathogen_name = Column(String(255), nullable=False, index=True)
    antibiotic_name = Column(String(255), nullable=False, index=True)
    country_iso3 = Column(String(3), nullable=False, index=True)
    region_who = Column(String(50), nullable=True)
    year = Column(SmallInteger, nullable=False)
    resistance_rate = Column(Float, nullable=True)
    sample_count = Column(Integer, nullable=True)
    data_source = Column(String(50), nullable=True)

    # Tier at this point in time
    tier = Column(String(20), nullable=False)  # STABLE/WATCH/EMERGING/CRITICAL/IMPROVING

    # Context for the transition
    previous_tier = Column(String(20), nullable=True)  # NULL for first observation
    tier_changed = Column(Boolean, nullable=False, default=False)
    years_in_previous_tier = Column(SmallInteger, nullable=True)

    # Quantitative signals
    rate_change_1yr = Column(Float, nullable=True)   # absolute change vs prior year
    rate_change_3yr = Column(Float, nullable=True)   # absolute change vs 3 years prior
    acceleration = Column(Float, nullable=True)       # 2nd derivative (rate of rate change)

    computed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "pathogen_name", "antibiotic_name", "country_iso3", "year",
            name="uq_state_transition_triplet_year"
        ),
        Index("ix_state_transitions_triplet",
              "pathogen_name", "antibiotic_name", "country_iso3"),
        Index("ix_state_transitions_tier", "tier"),
        Index("ix_state_transitions_year", "year"),
        Index("ix_state_transitions_changed", "tier_changed"),
        Index("ix_state_transitions_country_year", "country_iso3", "year"),
    )

    def __repr__(self) -> str:
        return (
            f"<StateTransition {self.pathogen_name}/{self.antibiotic_name}/"
            f"{self.country_iso3} year={self.year} tier={self.tier}>"
        )