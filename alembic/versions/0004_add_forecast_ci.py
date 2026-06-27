"""Add forecast confidence interval columns to alerts table

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-25

Adds four CI columns to the alerts table:
    forecast_lower_80 — p10 quantile forecast (80% CI lower bound)
    forecast_upper_80 — p90 quantile forecast (80% CI upper bound)
    forecast_lower_50 — p25 quantile forecast (50% CI lower bound)
    forecast_upper_50 — p75 quantile forecast (50% CI upper bound)

A narrow CI with large deviation = strong, high-confidence signal.
A wide CI with large deviation = uncertain signal, needs more data.
These are surfaced in the dashboard trend chart as a shaded band.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alerts", sa.Column("forecast_lower_80", sa.Float(), nullable=True))
    op.add_column("alerts", sa.Column("forecast_upper_80", sa.Float(), nullable=True))
    op.add_column("alerts", sa.Column("forecast_lower_50", sa.Float(), nullable=True))
    op.add_column("alerts", sa.Column("forecast_upper_50", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("alerts", "forecast_upper_50")
    op.drop_column("alerts", "forecast_lower_50")
    op.drop_column("alerts", "forecast_upper_80")
    op.drop_column("alerts", "forecast_lower_80")