"""
Alembic migration 0007 — Add updated_at column to alerts table.

updated_at tracks the last pipeline confirmation that a signal is still active.
created_at (existing, immutable) = first detection date = lead-time anchor.
updated_at (new) = last confirmed active = what time window filters compare against.

Every daily pipeline run will now stamp updated_at = now() on every alert
it processes via ON CONFLICT DO UPDATE, preserving the continuous surveillance
record even when the underlying resistance data hasn't changed.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add updated_at column — defaults to created_at value for existing rows
    # so all current alerts show as "last confirmed" at their original detection date.
    op.add_column(
        "alerts",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,  # nullable during migration; set to NOT NULL after backfill
        ),
    )

    # Backfill: set updated_at = created_at for all existing alerts.
    # This preserves the historical record — existing alerts are treated as
    # "last confirmed" at the time they were first detected.
    op.execute("""
        UPDATE alerts
        SET updated_at = created_at
        WHERE updated_at IS NULL
    """)

    # Now make it NOT NULL with server default going forward
    op.alter_column("alerts", "updated_at", nullable=False,
                    server_default=sa.text("now()"))

    # Index for efficient time window queries
    op.create_index("ix_alerts_updated_at", "alerts", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_alerts_updated_at", table_name="alerts")
    op.drop_column("alerts", "updated_at")