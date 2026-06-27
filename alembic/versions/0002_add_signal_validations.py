"""Add signal_validations table for lead-time tracking

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-24

This migration adds the signal_validations table — the mechanism for
tracking AMR-Sentinel's predictive lead time against official surveillance
bodies. This is the headline Abuja demo metric.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_validations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "alert_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("alerts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # When AMR-Sentinel detected it
        sa.Column("signal_detected_at", sa.DateTime(timezone=True), nullable=False),
        # When the official world recognised it
        sa.Column("official_recognition_source", sa.String(500), nullable=False),
        sa.Column("official_recognition_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("official_recognition_url", sa.String(1000), nullable=True),
        # The headline metric
        sa.Column("lead_time_days", sa.Integer(), nullable=False),
        # Validation details
        sa.Column("validation_status", sa.String(20), nullable=False, server_default="confirmed"),
        sa.Column("validated_resistance_rate", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("validated_by", sa.String(100), nullable=True),
        sa.Column(
            "validated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_index("ix_signal_validations_alert_id", "signal_validations", ["alert_id"])
    op.create_index("ix_signal_validations_lead_time", "signal_validations", ["lead_time_days"])
    op.create_index("ix_signal_validations_status", "signal_validations", ["validation_status"])


def downgrade() -> None:
    op.drop_table("signal_validations")