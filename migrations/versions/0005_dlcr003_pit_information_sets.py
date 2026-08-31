"""Add DLCR-003 semantic PIT information-set persistence.

Revision ID: 0005_dlcr003_pit_sets
Revises: 0004_stage2b_bea
Create Date: 2026-08-31

Existing exact PIT snapshots and audit rows remain unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_dlcr003_pit_sets"
down_revision: str | None = "0004_stage2b_bea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add immutable index generations and semantic information-set headers."""
    op.create_table(
        "pit_change_index_generations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("indicator_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_series_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("index_coverage_start", sa.Date(), nullable=False),
        sa.Column("index_coverage_end", sa.Date(), nullable=False),
        sa.Column("index_complete_through", sa.Date()),
        sa.Column("provider_count", sa.Integer(), nullable=False),
        sa.Column("retrieved_count", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("change_dates_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "pagination_evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("generation_hash", sa.String(length=64), nullable=False),
        sa.Column("source_registry_hash", sa.String(length=64), nullable=False),
        sa.Column("pit_materialization_spec_version", sa.String(length=64), nullable=False),
        sa.Column("pit_materialization_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("sync_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("supersedes_generation_id", sa.BigInteger()),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("page_count >= 0", name="page_count_nonnegative"),
        sa.CheckConstraint("provider_count >= 0", name="provider_count_nonnegative"),
        sa.CheckConstraint(
            "retrieved_count >= 0",
            name="retrieved_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["indicator_id"],
            ["indicator_registry.indicator_id"],
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_generation_id"],
            ["pit_change_index_generations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "indicator_id",
            "provider",
            "provider_series_id",
            "generation_hash",
            name="uq_pit_change_index_generation_identity",
        ),
    )
    op.create_index(
        "ix_pit_change_index_generation_lookup",
        "pit_change_index_generations",
        [
            "indicator_id",
            "provider",
            "provider_series_id",
            "status",
            "index_complete_through",
        ],
    )

    op.create_table(
        "pit_information_sets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("indicator_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_series_id", sa.String(length=128), nullable=False),
        sa.Column("source_information_set_date", sa.Date(), nullable=False),
        sa.Column("information_set_hash", sa.String(length=64), nullable=False),
        sa.Column("official_source_contract_identity", sa.String(length=255), nullable=False),
        sa.Column("source_registry_hash", sa.String(length=64), nullable=False),
        sa.Column("pit_materialization_spec_version", sa.String(length=64), nullable=False),
        sa.Column("pit_materialization_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("index_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("storage_mode", sa.String(length=32), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column(
            "missing_observation_dates_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("earliest_observation_date", sa.Date()),
        sa.Column("latest_observation_date", sa.Date()),
        sa.Column("raw_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("legacy_vintage_date", sa.Date()),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sync_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_by_id", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("observation_count >= 0", name="observation_count_nonnegative"),
        sa.ForeignKeyConstraint(
            ["index_generation_id"],
            ["pit_change_index_generations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["indicator_id"],
            ["indicator_registry.indicator_id"],
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"], ["pit_information_sets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "indicator_id",
            "provider",
            "provider_series_id",
            "source_information_set_date",
            "source_registry_hash",
            "pit_materialization_spec_hash",
            "information_set_hash",
            name="uq_pit_information_set_identity",
        ),
    )
    op.create_index(
        "ix_pit_information_set_lookup",
        "pit_information_sets",
        [
            "indicator_id",
            "provider",
            "provider_series_id",
            "source_information_set_date",
            "status",
        ],
    )
    op.create_index("ix_pit_information_set_hash", "pit_information_sets", ["information_set_hash"])

    op.add_column("observation_vintages", sa.Column("pit_information_set_id", sa.BigInteger()))
    op.create_foreign_key(
        "fk_obs_vintage_pit_info_set",
        "observation_vintages",
        "pit_information_sets",
        ["pit_information_set_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_observation_vintages_information_set_date",
        "observation_vintages",
        ["pit_information_set_id", "observation_date"],
        unique=True,
        postgresql_where=sa.text("pit_information_set_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove DLCR-003 structures without touching legacy observation or audit rows."""
    op.drop_index("uq_observation_vintages_information_set_date", table_name="observation_vintages")
    op.drop_constraint(
        "fk_obs_vintage_pit_info_set",
        "observation_vintages",
        type_="foreignkey",
    )
    op.drop_column("observation_vintages", "pit_information_set_id")
    op.drop_index("ix_pit_information_set_hash", table_name="pit_information_sets")
    op.drop_index("ix_pit_information_set_lookup", table_name="pit_information_sets")
    op.drop_table("pit_information_sets")
    op.drop_index(
        "ix_pit_change_index_generation_lookup", table_name="pit_change_index_generations"
    )
    op.drop_table("pit_change_index_generations")
