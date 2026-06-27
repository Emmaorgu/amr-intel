"""Add emergence_scores table — v2 with confidence and acceleration_index

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "emergence_scores",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("pathogen_name", sa.String(255), nullable=False),
        sa.Column("antibiotic_name", sa.String(255), nullable=False),
        sa.Column("country_iso3", sa.String(3), nullable=False),
        sa.Column("region_who", sa.String(50), nullable=True),
        sa.Column("emergence_score", sa.SmallInteger(), nullable=False),
        # Classification: emerging | endemic_critical | improving | watch | stable
        sa.Column("emergence_tier", sa.String(20), nullable=False),
        sa.Column("acceleration_score", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("new_appearance_score", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("spread_velocity_score", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("endemic_penalty", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("acceleration_rate", sa.Float(), nullable=True),
        # 1-10 capped acceleration index — operationally meaningful
        sa.Column("acceleration_index", sa.SmallInteger(), nullable=True),
        sa.Column("baseline_rate", sa.Float(), nullable=True),
        sa.Column("current_rate", sa.Float(), nullable=True),
        sa.Column("years_observed", sa.SmallInteger(), nullable=True),
        # Confidence: high | medium | low
        sa.Column("data_confidence", sa.String(10), nullable=True),
        sa.Column("has_baseline", sa.Boolean(), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index("ix_emergence_scores_score", "emergence_scores", ["emergence_score"])
    op.create_index("ix_emergence_scores_tier", "emergence_scores", ["emergence_tier"])
    op.create_index("ix_emergence_scores_country", "emergence_scores", ["country_iso3"])
    op.create_index("ix_emergence_scores_confidence", "emergence_scores", ["data_confidence"])
    op.create_index(
        "ix_emergence_scores_triplet",
        "emergence_scores",
        ["pathogen_name", "antibiotic_name", "country_iso3"],
    )


def downgrade() -> None:
    op.drop_table("emergence_scores")