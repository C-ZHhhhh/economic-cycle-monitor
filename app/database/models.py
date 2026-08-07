"""SQLAlchemy models for macro-agent and Stage 2 FRED persistence."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, utc_now

INDICATOR_FK = "indicator_registry.indicator_id"
RUN_FK = "agent_runs.run_id"
SYNC_RUN_FK = "sync_runs.run_id"


class TimestampMixin:
    """UTC creation timestamp shared by immutable records."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )


class IndicatorRegistry(TimestampMixin, Base):
    """Versioned registry of economic indicators and their source metadata."""

    __tablename__ = "indicator_registry"
    __table_args__ = (
        Index("ix_indicator_registry_module_enabled", "module", "enabled"),
        Index("ix_indicator_registry_metadata_status", "metadata_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    indicator_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    module: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_agent: Mapped[str] = mapped_column(String(128), nullable=False)
    fred_series_id: Mapped[str | None] = mapped_column(String(128))
    primary_source: Mapped[str] = mapped_column(String(128), nullable=False)
    primary_source_series_id: Mapped[str | None] = mapped_column(String(128))
    fallback_source: Mapped[str | None] = mapped_column(String(128))
    observation_frequency: Mapped[str] = mapped_column(String(64), nullable=False)
    release_frequency: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(128), nullable=False)
    seasonal_adjustment: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False)
    max_staleness_days: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_policy: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_status: Mapped[str] = mapped_column(
        String(32), default="not_checked", server_default="not_checked", nullable=False
    )
    metadata_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_title: Mapped[str | None] = mapped_column(String(512))
    source_description: Mapped[str | None] = mapped_column(Text)
    source_notes: Mapped[str | None] = mapped_column(Text)
    source_frequency: Mapped[str | None] = mapped_column(String(128))
    source_units: Mapped[str | None] = mapped_column(String(255))
    source_seasonal_adjustment: Mapped[str | None] = mapped_column(String(255))
    source_observation_start: Mapped[date | None] = mapped_column(Date)
    source_observation_end: Mapped[date | None] = mapped_column(Date)
    source_last_updated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_popularity: Mapped[int | None] = mapped_column(Integer)
    source_release_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    metadata_warnings_json: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
        nullable=False,
    )


class RawObservation(TimestampMixin, Base):
    """Latest raw observation retrieved from a named source."""

    __tablename__ = "raw_observations"
    __table_args__ = (
        UniqueConstraint(
            "indicator_id", "observation_date", "source", name="uq_raw_observation_key"
        ),
        Index("ix_raw_observations_observation_date", "observation_date"),
        Index("ix_raw_observations_retrieved_at", "retrieved_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    indicator_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(INDICATOR_FK, onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
    )
    observation_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_vintage_date: Mapped[date | None] = mapped_column(Date)
    source_details_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )


class ObservationVintage(TimestampMixin, Base):
    """Point-in-time release and revision history for an observation."""

    __tablename__ = "observation_vintages"
    __table_args__ = (
        UniqueConstraint(
            "indicator_id",
            "observation_date",
            "vintage_date",
            "revision_number",
            "source",
            name="uq_observation_vintage_key",
        ),
        CheckConstraint("revision_number >= 0", name="revision_number_nonnegative"),
        Index("ix_observation_vintages_observation_date", "observation_date"),
        Index("ix_observation_vintages_available_at", "available_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    indicator_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(INDICATOR_FK, onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
    )
    observation_date: Mapped[date] = mapped_column(Date, nullable=False)
    vintage_date: Mapped[date] = mapped_column(Date, nullable=False)
    release_date: Mapped[date | None] = mapped_column(Date)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    current_value: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    source: Mapped[str] = mapped_column(
        String(128), default="fred", server_default="fred", nullable=False
    )
    source_vintage_date: Mapped[date | None] = mapped_column(Date)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload_hash: Mapped[str | None] = mapped_column(String(64))
    source_details_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )


class IndicatorSourceMapping(TimestampMixin, Base):
    """Versioned mapping from an indicator to one official-source series."""

    __tablename__ = "indicator_source_mappings"
    __table_args__ = (
        UniqueConstraint(
            "indicator_id",
            "source",
            "source_series_id",
            "mapping_version",
            name="uq_indicator_source_mapping_version",
        ),
        Index("ix_source_mappings_source_enabled", "source", "enabled"),
        Index("ix_source_mappings_indicator_priority", "indicator_id", "source_priority"),
        Index("ix_source_mappings_status", "mapping_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    indicator_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(INDICATOR_FK, onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_series_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    source_parameters_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    source_name: Mapped[str] = mapped_column(String(512), nullable=False)
    source_unit: Mapped[str] = mapped_column(String(128), nullable=False)
    source_frequency: Mapped[str] = mapped_column(String(64), nullable=False)
    source_seasonal_adjustment: Mapped[str] = mapped_column(String(64), nullable=False)
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    fallback_source: Mapped[str | None] = mapped_column(String(64))
    vintage_source: Mapped[str | None] = mapped_column(String(64))
    normalization_rule: Mapped[str] = mapped_column(String(128), nullable=False)
    comparison_tolerance: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    distribution_lag_days: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_lookback_years: Mapped[int] = mapped_column(Integer, nullable=False)
    mapping_status: Mapped[str] = mapped_column(String(32), nullable=False)
    mapping_warnings_json: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    mapping_version: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
        nullable=False,
    )


class SourceComparisonResult(TimestampMixin, Base):
    """Auditable point comparison between two independently stored sources."""

    __tablename__ = "source_comparison_results"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "indicator_id",
            "observation_date",
            "primary_source",
            "secondary_source",
            name="uq_source_comparison_run_point",
        ),
        Index("ix_source_comparisons_indicator_date", "indicator_id", "observation_date"),
        Index("ix_source_comparisons_run_status", "run_id", "comparison_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(SYNC_RUN_FK, ondelete="SET NULL")
    )
    indicator_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(INDICATOR_FK, onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    observation_date: Mapped[date] = mapped_column(Date, nullable=False)
    primary_source: Mapped[str] = mapped_column(String(64), nullable=False)
    secondary_source: Mapped[str] = mapped_column(String(64), nullable=False)
    primary_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    secondary_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    normalized_primary_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    normalized_secondary_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    absolute_difference: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    relative_difference: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    tolerance: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    comparison_status: Mapped[str] = mapped_column(String(64), nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )


class SyncRun(TimestampMixin, Base):
    """Auditable execution and counters for one data-pipeline operation."""

    __tablename__ = "sync_runs"
    __table_args__ = (
        Index("ix_sync_runs_operation_started", "operation", "started_at"),
        Index("ix_sync_runs_status_started", "status", "started_at"),
    )

    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    indicator_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revised_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quality_event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )


class DataSourceRequest(TimestampMixin, Base):
    """Secret-free audit record for one logical data-source request."""

    __tablename__ = "data_source_requests"
    __table_args__ = (
        Index("ix_data_source_requests_source_started", "source", "started_at"),
        Index("ix_data_source_requests_success_started", "success", "started_at"),
        Index("ix_data_source_requests_series_id", "series_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sync_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(SYNC_RUN_FK, ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    series_id: Mapped[str | None] = mapped_column(String(128))
    request_parameters_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    response_hash: Mapped[str | None] = mapped_column(String(64))
    response_path: Mapped[str | None] = mapped_column(Text)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)


class DataQualityEvent(TimestampMixin, Base):
    """A detected data or metadata quality issue and its resolution state."""

    __tablename__ = "data_quality_events"
    __table_args__ = (
        Index("ix_data_quality_events_indicator_detected", "indicator_id", "detected_at"),
        Index("ix_data_quality_events_type_severity", "event_type", "severity"),
        Index("ix_data_quality_events_resolved_at", "resolved_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(SYNC_RUN_FK, ondelete="SET NULL")
    )
    indicator_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey(INDICATOR_FK, onupdate="CASCADE", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    event_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_notes: Mapped[str | None] = mapped_column(Text)


class DerivedFeature(TimestampMixin, Base):
    """Versioned derived features calculated from one indicator."""

    __tablename__ = "derived_features"
    __table_args__ = (
        UniqueConstraint(
            "indicator_id",
            "feature_date",
            "calculation_version",
            name="uq_derived_feature_key",
        ),
        Index("ix_derived_features_feature_date", "feature_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    indicator_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(INDICATOR_FK, onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
    )
    feature_date: Mapped[date] = mapped_column(Date, nullable=False)
    mom: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    yoy: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    annualized_3m: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    annualized_6m: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    trend_slope: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    robust_zscore: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    historical_percentile: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    turning_point: Mapped[str | None] = mapped_column(String(64))
    freshness_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    calculation_version: Mapped[str] = mapped_column(String(32), nullable=False)


class AgentRun(TimestampMixin, Base):
    """Auditable execution record for an agent invocation."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finish_after_start"
        ),
        Index("ix_agent_runs_agent_started", "agent_name", "started_at"),
        Index("ix_agent_runs_status_started", "status", "started_at"),
    )

    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    input_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    data_quality_status: Mapped[str | None] = mapped_column(String(32))
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    llm_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    git_commit: Mapped[str | None] = mapped_column(String(64))


class ModuleScore(TimestampMixin, Base):
    """Versioned score for one analysis module within an agent run."""

    __tablename__ = "module_scores"
    __table_args__ = (
        UniqueConstraint("run_id", "module_name", "scoring_version", name="uq_module_score_key"),
        Index("ix_module_scores_module_created", "module_name", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(RUN_FK, ondelete="CASCADE"), nullable=False
    )
    module_name: Mapped[str] = mapped_column(String(128), nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False)
    direction: Mapped[str] = mapped_column(String(64), nullable=False)
    positive_breadth: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    contributors_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    detractors_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    scoring_version: Mapped[str] = mapped_column(String(32), nullable=False)


class RegimeHistory(TimestampMixin, Base):
    """Versioned macro-regime state emitted by an agent run."""

    __tablename__ = "regime_history"
    __table_args__ = (
        UniqueConstraint("run_id", "regime_engine_version", name="uq_regime_history_key"),
        Index("ix_regime_history_regime_started", "regime", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(RUN_FK, ondelete="CASCADE"), nullable=False
    )
    regime: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_regime: Mapped[str | None] = mapped_column(String(64))
    regime_probabilities_json: Mapped[dict[str, float]] = mapped_column(JSONB, nullable=False)
    process_labels_json: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    regime_engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    transition_reason_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentReport(TimestampMixin, Base):
    """Structured and rendered output associated with an agent run."""

    __tablename__ = "agent_reports"
    __table_args__ = (
        UniqueConstraint("run_id", "report_type", name="uq_agent_report_key"),
        Index("ix_agent_reports_type_created", "report_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(RUN_FK, ondelete="CASCADE"), nullable=False
    )
    report_type: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    markdown_content: Mapped[str | None] = mapped_column(Text)
    html_content: Mapped[str | None] = mapped_column(Text)
    llm_provider: Mapped[str | None] = mapped_column(String(64))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
