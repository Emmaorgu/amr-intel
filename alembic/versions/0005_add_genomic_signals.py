"""Add genomic_signals table for NCBI NDARO resistance gene data

Revision ID: 0005
Revises: 0003
Create Date: 2026-06-27

genomic_signals stores pre-phenotypic resistance gene signals from NCBI
Pathogen Detection (NDARO). Each row represents the count of isolates
carrying a priority resistance gene (NDM-1, OXA-48, MCR-1, KPC, VIM etc.)
in a specific country and year.

This is distinct from resistance_records (phenotypic resistance rates).
Genomic signals represent early warning: a gene detected before clinical
phenotypic resistance appears in surveillance data.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "genomic_signals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gene_name", sa.Text(), nullable=False),
        sa.Column("gene_family", sa.Text(), nullable=False),
        sa.Column("drug_class", sa.Text(), nullable=False),
        sa.Column("pathogen_name", sa.Text(), nullable=False),
        sa.Column("country_iso3", sa.Text(), nullable=False),
        sa.Column("region_who", sa.Text(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("isolate_count", sa.Integer(), nullable=False),
        sa.Column("data_source", sa.Text(), server_default="NCBI_NDARO"),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "gene_name", "pathogen_name", "country_iso3", "year",
            name="uq_genomic_signal"
        ),
    )
    op.create_index(
        "ix_genomic_signals_country_year",
        "genomic_signals",
        ["country_iso3", "year"],
    )
    op.create_index(
        "ix_genomic_signals_gene_family",
        "genomic_signals",
        ["gene_family"],
    )


def downgrade() -> None:
    op.drop_index("ix_genomic_signals_gene_family", table_name="genomic_signals")
    op.drop_index("ix_genomic_signals_country_year", table_name="genomic_signals")
    op.drop_table("genomic_signals")