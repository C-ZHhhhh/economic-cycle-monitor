"""SQLAlchemy models for Stage 1 macro-agent persistence."""

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


class TimestampMixin:
    """UTC creation timestamp shared by immutable records."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )


class IndicatorRegistry(TimestampMixin, Base):
    """Versioned registry of economic indicators and their source metadata."""

    __tablename__ = "indicator_registry"
    __table_args__ = (Index("ix_indicator_registry_module_enabled", "module", "enabled"),)

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


class ObservationVintage(TimestampMixin, Base):
    """Point-in-time release and revision history for an observation."""

    __tablename__ = "observation_vintages"
    __table_args__ = (
        UniqueConstraint(
            "indicator_id",
            "observation_date",
            "vintage_date",
            "revision_number",
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
