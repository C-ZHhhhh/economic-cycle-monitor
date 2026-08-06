"""Add Stage 2 FRED ingestion, metadata, and data-quality schema.

Revision ID: 0002_stage2_fred_pipeline
Revises: 0001_initial_schema
Create Date: 2026-08-06

Downgrading removes Stage 2 request, sync, quality, and metadata data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_stage2_fred_pipeline"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Stage 2 metadata, request audit, sync, and quality structures."""
    op.add_column(
        "indicator_registry",
        sa.Column(
            "metadata_status",
            sa.String(length=32),
            server_default="not_checked",
            nullable=False,
        ),
    )
    op.add_column(
        "indicator_registry", sa.Column("metadata_checked_at", sa.DateTime(timezone=True))
    )
    op.add_column("indicator_registry", sa.Column("source_title", sa.String(length=512)))
    op.add_column("indicator_registry", sa.Column("source_description", sa.Text()))
    op.add_column("indicator_registry", sa.Column("source_notes", sa.Text()))
    op.add_column("indicator_registry", sa.Column("source_frequency", sa.String(length=128)))
    op.add_column("indicator_registry", sa.Column("source_units", sa.String(length=255)))
    op.add_column(
        "indicator_registry", sa.Column("source_seasonal_adjustment", sa.String(length=255))
    )
    op.add_column("indicator_registry", sa.Column("source_observation_start", sa.Date()))
    op.add_column("indicator_registry", sa.Column("source_observation_end", sa.Date()))
    op.add_column(
        "indicator_registry", sa.Column("source_last_updated", sa.DateTime(timezone=True))
    )
    op.add_column("indicator_registry", sa.Column("source_popularity", sa.Integer()))
    op.add_column(
        "indicator_registry",
        sa.Column("source_release_json", postgresql.JSONB(astext_type=sa.Text())),
    )
    op.add_column(
        "indicator_registry",
        sa.Column(
            "metadata_warnings_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_indicator_registry_metadata_status",
        "indicator_registry",
        ["metadata_status"],
    )

    op.add_column("raw_observations", sa.Column("source_vintage_date", sa.Date()))
    op.drop_constraint("uq_observation_vintage_key", "observation_vintages", type_="unique")
    op.add_column(
        "observation_vintages",
        sa.Column("source", sa.String(length=128), server_default="fred", nullable=False),
    )
    op.add_column("observation_vintages", sa.Column("source_vintage_date", sa.Date()))
    op.add_column("observation_vintages", sa.Column("retrieved_at", sa.DateTime(timezone=True)))
    op.add_column("observation_vintages", sa.Column("raw_payload_hash", sa.String(length=64)))
    op.create_unique_constraint(
        "uq_observation_vintage_key",
        "observation_vintages",
        ["indicator_id", "observation_date", "vintage_date", "revision_number", "source"],
    )

    op.create_table(
        "sync_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("parameters_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("indicator_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("inserted_count", sa.Integer(), nullable=False),
        sa.Column("revised_count", sa.Integer(), nullable=False),
        sa.Column("quality_event_count", sa.Integer(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("cache_hit_count", sa.Integer(), nullable=False),
        sa.Column("error_summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_sync_runs")),
    )
    op.create_index("ix_sync_runs_operation_started", "sync_runs", ["operation", "started_at"])
    op.create_index("ix_sync_runs_status_started", "sync_runs", ["status", "started_at"])

    op.create_table(
        "data_source_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("sync_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=255), nullable=False),
        sa.Column("series_id", sa.String(length=128)),
        sa.Column(
            "request_parameters_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("response_hash", sa.String(length=64)),
        sa.Column("response_path", sa.Text()),
        sa.Column("from_cache", sa.Boolean(), nullable=False),
        sa.Column("error_type", sa.String(length=128)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["sync_run_id"],
            ["sync_runs.run_id"],
            name=op.f("fk_data_source_requests_sync_run_id_sync_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_source_requests")),
    )
    op.create_index(
        "ix_data_source_requests_source_started",
        "data_source_requests",
        ["source", "started_at"],
    )
    op.create_index(
        "ix_data_source_requests_success_started",
        "data_source_requests",
        ["success", "started_at"],
    )
    op.create_index("ix_data_source_requests_series_id", "data_source_requests", ["series_id"])

    op.create_table(
        "data_quality_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("indicator_id", sa.String(length=128)),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("event_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_notes", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["indicator_id"],
            ["indicator_registry.indicator_id"],
            name=op.f("fk_data_quality_events_indicator_id_indicator_registry"),
            onupdate="CASCADE",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sync_runs.run_id"],
            name=op.f("fk_data_quality_events_run_id_sync_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_quality_events")),
    )
    op.create_index(
        "ix_data_quality_events_indicator_detected",
        "data_quality_events",
        ["indicator_id", "detected_at"],
    )
    op.create_index(
        "ix_data_quality_events_type_severity",
        "data_quality_events",
        ["event_type", "severity"],
    )
    op.create_index("ix_data_quality_events_resolved_at", "data_quality_events", ["resolved_at"])


def downgrade() -> None:
    """Remove all Stage 2 structures and restore Stage 1 constraints."""
    op.drop_index("ix_data_quality_events_resolved_at", table_name="data_quality_events")
    op.drop_index("ix_data_quality_events_type_severity", table_name="data_quality_events")
    op.drop_index("ix_data_quality_events_indicator_detected", table_name="data_quality_events")
    op.drop_table("data_quality_events")
    op.drop_index("ix_data_source_requests_series_id", table_name="data_source_requests")
    op.drop_index("ix_data_source_requests_success_started", table_name="data_source_requests")
    op.drop_index("ix_data_source_requests_source_started", table_name="data_source_requests")
    op.drop_table("data_source_requests")
    op.drop_index("ix_sync_runs_status_started", table_name="sync_runs")
    op.drop_index("ix_sync_runs_operation_started", table_name="sync_runs")
    op.drop_table("sync_runs")

    op.drop_constraint("uq_observation_vintage_key", "observation_vintages", type_="unique")
    op.drop_column("observation_vintages", "raw_payload_hash")
    op.drop_column("observation_vintages", "retrieved_at")
    op.drop_column("observation_vintages", "source_vintage_date")
    op.drop_column("observation_vintages", "source")
    op.create_unique_constraint(
        "uq_observation_vintage_key",
        "observation_vintages",
        ["indicator_id", "observation_date", "vintage_date", "revision_number"],
    )
    op.drop_column("raw_observations", "source_vintage_date")

    op.drop_index("ix_indicator_registry_metadata_status", table_name="indicator_registry")
    for column_name in (
        "metadata_warnings_json",
        "source_release_json",
        "source_popularity",
        "source_last_updated",
        "source_observation_end",
        "source_observation_start",
        "source_seasonal_adjustment",
        "source_units",
        "source_frequency",
        "source_notes",
        "source_description",
        "source_title",
        "metadata_checked_at",
        "metadata_status",
    ):
        op.drop_column("indicator_registry", column_name)
