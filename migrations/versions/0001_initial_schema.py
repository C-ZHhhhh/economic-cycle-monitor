"""Create Stage 1 macro-agent tables.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the initial Stage 1 schema."""
    op.create_table(
        "indicator_registry",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("indicator_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("module", sa.String(length=128), nullable=False),
        sa.Column("owner_agent", sa.String(length=128), nullable=False),
        sa.Column("fred_series_id", sa.String(length=128), nullable=True),
        sa.Column("primary_source", sa.String(length=128), nullable=False),
        sa.Column("primary_source_series_id", sa.String(length=128), nullable=True),
        sa.Column("fallback_source", sa.String(length=128), nullable=True),
        sa.Column("observation_frequency", sa.String(length=64), nullable=False),
        sa.Column("release_frequency", sa.String(length=64), nullable=False),
        sa.Column("unit", sa.String(length=128), nullable=False),
        sa.Column("seasonal_adjustment", sa.String(length=128), nullable=False),
        sa.Column("direction", sa.String(length=64), nullable=False),
        sa.Column("weight", sa.Numeric(precision=12, scale=8), nullable=False),
        sa.Column("max_staleness_days", sa.Integer(), nullable=False),
        sa.Column("revision_policy", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("config_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_indicator_registry")),
        sa.UniqueConstraint("indicator_id", name=op.f("uq_indicator_registry_indicator_id")),
    )
    op.create_index(
        "ix_indicator_registry_module_enabled",
        "indicator_registry",
        ["module", "enabled"],
        unique=False,
    )

    op.create_table(
        "agent_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_name", sa.String(length=128), nullable=False),
        sa.Column("agent_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("input_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("data_quality_status", sa.String(length=32), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("llm_mode", sa.String(length=16), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("git_commit", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_agent_runs_finish_after_start"),
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_agent_runs")),
    )
    op.create_index("ix_agent_runs_agent_started", "agent_runs", ["agent_name", "started_at"])
    op.create_index("ix_agent_runs_status_started", "agent_runs", ["status", "started_at"])

    op.create_table(
        "raw_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("indicator_id", sa.String(length=128), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["indicator_id"],
            ["indicator_registry.indicator_id"],
            name=op.f("fk_raw_observations_indicator_id_indicator_registry"),
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_observations")),
        sa.UniqueConstraint(
            "indicator_id", "observation_date", "source", name="uq_raw_observation_key"
        ),
    )
    op.create_index(
        "ix_raw_observations_observation_date", "raw_observations", ["observation_date"]
    )
    op.create_index("ix_raw_observations_retrieved_at", "raw_observations", ["retrieved_at"])

    op.create_table(
        "observation_vintages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("indicator_id", sa.String(length=128), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("vintage_date", sa.Date(), nullable=False),
        sa.Column("release_date", sa.Date(), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_stage", sa.String(length=64), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("previous_value", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("current_value", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision_number >= 0", name=op.f("ck_observation_vintages_revision_number_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["indicator_id"],
            ["indicator_registry.indicator_id"],
            name=op.f("fk_observation_vintages_indicator_id_indicator_registry"),
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observation_vintages")),
        sa.UniqueConstraint(
            "indicator_id",
            "observation_date",
            "vintage_date",
            "revision_number",
            name="uq_observation_vintage_key",
        ),
    )
    op.create_index(
        "ix_observation_vintages_available_at", "observation_vintages", ["available_at"]
    )
    op.create_index(
        "ix_observation_vintages_observation_date",
        "observation_vintages",
        ["observation_date"],
    )

    op.create_table(
        "derived_features",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("indicator_id", sa.String(length=128), nullable=False),
        sa.Column("feature_date", sa.Date(), nullable=False),
        sa.Column("mom", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("yoy", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("annualized_3m", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("annualized_6m", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("trend_slope", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("robust_zscore", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("historical_percentile", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("turning_point", sa.String(length=64), nullable=True),
        sa.Column("freshness_score", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("calculation_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["indicator_id"],
            ["indicator_registry.indicator_id"],
            name=op.f("fk_derived_features_indicator_id_indicator_registry"),
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_derived_features")),
        sa.UniqueConstraint(
            "indicator_id", "feature_date", "calculation_version", name="uq_derived_feature_key"
        ),
    )
    op.create_index("ix_derived_features_feature_date", "derived_features", ["feature_date"])

    op.create_table(
        "module_scores",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("module_name", sa.String(length=128), nullable=False),
        sa.Column("score", sa.Numeric(precision=12, scale=8), nullable=False),
        sa.Column("direction", sa.String(length=64), nullable=False),
        sa.Column("positive_breadth", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("contributors_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("detractors_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scoring_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.run_id"],
            name=op.f("fk_module_scores_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_module_scores")),
        sa.UniqueConstraint("run_id", "module_name", "scoring_version", name="uq_module_score_key"),
    )
    op.create_index(
        "ix_module_scores_module_created", "module_scores", ["module_name", "created_at"]
    )

    op.create_table(
        "regime_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("regime", sa.String(length=64), nullable=False),
        sa.Column("previous_regime", sa.String(length=64), nullable=True),
        sa.Column(
            "regime_probabilities_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("process_labels_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("regime_engine_version", sa.String(length=32), nullable=False),
        sa.Column(
            "transition_reason_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.run_id"],
            name=op.f("fk_regime_history_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_regime_history")),
        sa.UniqueConstraint("run_id", "regime_engine_version", name="uq_regime_history_key"),
    )
    op.create_index("ix_regime_history_regime_started", "regime_history", ["regime", "started_at"])

    op.create_table(
        "agent_reports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_type", sa.String(length=64), nullable=False),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("markdown_content", sa.Text(), nullable=True),
        sa.Column("html_content", sa.Text(), nullable=True),
        sa.Column("llm_provider", sa.String(length=64), nullable=True),
        sa.Column("llm_model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.run_id"],
            name=op.f("fk_agent_reports_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_reports")),
        sa.UniqueConstraint("run_id", "report_type", name="uq_agent_report_key"),
    )
    op.create_index("ix_agent_reports_type_created", "agent_reports", ["report_type", "created_at"])


def downgrade() -> None:
    """Drop the initial Stage 1 schema in dependency order."""
    op.drop_index("ix_agent_reports_type_created", table_name="agent_reports")
    op.drop_table("agent_reports")
    op.drop_index("ix_regime_history_regime_started", table_name="regime_history")
    op.drop_table("regime_history")
    op.drop_index("ix_module_scores_module_created", table_name="module_scores")
    op.drop_table("module_scores")
    op.drop_index("ix_derived_features_feature_date", table_name="derived_features")
    op.drop_table("derived_features")
    op.drop_index("ix_observation_vintages_observation_date", table_name="observation_vintages")
    op.drop_index("ix_observation_vintages_available_at", table_name="observation_vintages")
    op.drop_table("observation_vintages")
    op.drop_index("ix_raw_observations_retrieved_at", table_name="raw_observations")
    op.drop_index("ix_raw_observations_observation_date", table_name="raw_observations")
    op.drop_table("raw_observations")
    op.drop_index("ix_agent_runs_status_started", table_name="agent_runs")
    op.drop_index("ix_agent_runs_agent_started", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_indicator_registry_module_enabled", table_name="indicator_registry")
    op.drop_table("indicator_registry")
