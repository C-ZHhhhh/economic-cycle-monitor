"""Add reusable official-source mappings and comparisons.

Revision ID: 0003_stage2b_official_sources
Revises: 0002_stage2_fred_pipeline
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_stage2b_official_sources"
down_revision: str | None = "0002_stage2_fred_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add source-neutral mapping, comparison, and observation-detail storage."""
    empty_object = sa.text("'{}'::jsonb")
    op.add_column(
        "raw_observations",
        sa.Column(
            "source_details_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=empty_object,
            nullable=False,
        ),
    )
    op.add_column(
        "observation_vintages",
        sa.Column(
            "source_details_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=empty_object,
            nullable=False,
        ),
    )

    op.create_table(
        "indicator_source_mappings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("indicator_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_series_id", sa.String(length=128), nullable=False),
        sa.Column("source_dataset", sa.String(length=64), nullable=False),
        sa.Column("source_name", sa.String(length=512), nullable=False),
        sa.Column("source_unit", sa.String(length=128), nullable=False),
        sa.Column("source_frequency", sa.String(length=64), nullable=False),
        sa.Column("source_seasonal_adjustment", sa.String(length=64), nullable=False),
        sa.Column("source_priority", sa.Integer(), nullable=False),
        sa.Column("fallback_source", sa.String(length=64)),
        sa.Column("vintage_source", sa.String(length=64)),
        sa.Column("normalization_rule", sa.String(length=128), nullable=False),
        sa.Column("comparison_tolerance", sa.Numeric(30, 10), nullable=False),
        sa.Column("distribution_lag_days", sa.Integer(), nullable=False),
        sa.Column("revision_lookback_years", sa.Integer(), nullable=False),
        sa.Column("mapping_status", sa.String(length=32), nullable=False),
        sa.Column(
            "mapping_warnings_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("mapping_version", sa.String(length=32), nullable=False),
        sa.Column("metadata_checked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["indicator_id"],
            ["indicator_registry.indicator_id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "indicator_id",
            "source",
            "source_series_id",
            "mapping_version",
            name="uq_indicator_source_mapping_version",
        ),
    )
    op.create_index(
        "ix_source_mappings_source_enabled",
        "indicator_source_mappings",
        ["source", "enabled"],
    )
    op.create_index(
        "ix_source_mappings_indicator_priority",
        "indicator_source_mappings",
        ["indicator_id", "source_priority"],
    )
    op.create_index("ix_source_mappings_status", "indicator_source_mappings", ["mapping_status"])

    op.create_table(
        "source_comparison_results",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("indicator_id", sa.String(length=128), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("primary_source", sa.String(length=64), nullable=False),
        sa.Column("secondary_source", sa.String(length=64), nullable=False),
        sa.Column("primary_value", sa.Numeric(30, 10)),
        sa.Column("secondary_value", sa.Numeric(30, 10)),
        sa.Column("normalized_primary_value", sa.Numeric(30, 10)),
        sa.Column("normalized_secondary_value", sa.Numeric(30, 10)),
        sa.Column("absolute_difference", sa.Numeric(30, 10)),
        sa.Column("relative_difference", sa.Numeric(30, 10)),
        sa.Column("tolerance", sa.Numeric(30, 10), nullable=False),
        sa.Column("comparison_status", sa.String(length=64), nullable=False),
        sa.Column(
            "details_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=empty_object,
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["indicator_id"],
            ["indicator_registry.indicator_id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["sync_runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "indicator_id",
            "observation_date",
            "primary_source",
            "secondary_source",
            name="uq_source_comparison_run_point",
        ),
    )
    op.create_index(
        "ix_source_comparisons_indicator_date",
        "source_comparison_results",
        ["indicator_id", "observation_date"],
    )
    op.create_index(
        "ix_source_comparisons_run_status",
        "source_comparison_results",
        ["run_id", "comparison_status"],
    )


def downgrade() -> None:
    """Remove only Stage 2B official-source structures."""
    op.drop_index("ix_source_comparisons_run_status", table_name="source_comparison_results")
    op.drop_index("ix_source_comparisons_indicator_date", table_name="source_comparison_results")
    op.drop_table("source_comparison_results")
    op.drop_index("ix_source_mappings_status", table_name="indicator_source_mappings")
    op.drop_index("ix_source_mappings_indicator_priority", table_name="indicator_source_mappings")
    op.drop_index("ix_source_mappings_source_enabled", table_name="indicator_source_mappings")
    op.drop_table("indicator_source_mappings")
    op.drop_column("observation_vintages", "source_details_json")
    op.drop_column("raw_observations", "source_details_json")
