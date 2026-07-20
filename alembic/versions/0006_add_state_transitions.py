"""add state_transitions table

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'state_transitions',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('pathogen_name', sa.String(255), nullable=False),
        sa.Column('antibiotic_name', sa.String(255), nullable=False),
        sa.Column('country_iso3', sa.String(3), nullable=False),
        sa.Column('region_who', sa.String(50), nullable=True),
        sa.Column('year', sa.SmallInteger(), nullable=False),
        sa.Column('resistance_rate', sa.Float(), nullable=True),
        sa.Column('sample_count', sa.Integer(), nullable=True),
        sa.Column('data_source', sa.String(50), nullable=True),
        sa.Column('tier', sa.String(20), nullable=False),
        sa.Column('previous_tier', sa.String(20), nullable=True),
        sa.Column('tier_changed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('years_in_previous_tier', sa.SmallInteger(), nullable=True),
        sa.Column('rate_change_1yr', sa.Float(), nullable=True),
        sa.Column('rate_change_3yr', sa.Float(), nullable=True),
        sa.Column('acceleration', sa.Float(), nullable=True),
        sa.Column('computed_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('pathogen_name', 'antibiotic_name', 'country_iso3', 'year',
                            name='uq_state_transition_triplet_year'),
    )
    op.create_index('ix_state_transitions_triplet', 'state_transitions',
                    ['pathogen_name', 'antibiotic_name', 'country_iso3'])
    op.create_index('ix_state_transitions_tier', 'state_transitions', ['tier'])
    op.create_index('ix_state_transitions_year', 'state_transitions', ['year'])
    op.create_index('ix_state_transitions_changed', 'state_transitions', ['tier_changed'])
    op.create_index('ix_state_transitions_country_year', 'state_transitions',
                    ['country_iso3', 'year'])
    op.create_index('ix_state_transitions_pathogen', 'state_transitions', ['pathogen_name'])
    op.create_index('ix_state_transitions_antibiotic', 'state_transitions', ['antibiotic_name'])


def downgrade() -> None:
    op.drop_table('state_transitions')