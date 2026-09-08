"""Typed contracts for the versioned US macro explanation agent."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.composites.schemas import EvaluationMode, MacroStateResult, MacroStateStatus
from app.pillars.schemas import PillarResult
from app.regimes.schemas import (
    GrowthCondition,
    GrowthTrajectoryQualifier,
    InflationCondition,
    InflationTrajectoryQualifier,
    RegimeResult,
    RegimeStatus,
)
from app.signals.schemas import NormalizedSignalResult
from app.source_registry.schemas import HealthSnapshot, ReadinessStatus


class PersistedCadence(StrEnum):
    """Canonical schedules eligible for persisted comparison selection."""

    RELEASE_EVENT_EOD = "RELEASE_EVENT_EOD"
    MONTH_END_EOD = "MONTH_END_EOD"


class DependencyAvailability(StrEnum):
    """Typed local-only dependency result."""

    AVAILABLE = "AVAILABLE"
    UPSTREAM_RESULT_NOT_MATERIALIZED = "UPSTREAM_RESULT_NOT_MATERIALIZED"
    COMPARISON_NOT_AVAILABLE = "COMPARISON_NOT_AVAILABLE"
    TRAJECTORY_CONTEXT_NOT_AVAILABLE = "TRAJECTORY_CONTEXT_NOT_AVAILABLE"
    AUTHORITY_MISMATCH = "AUTHORITY_MISMATCH"


class FreshnessApplicability(StrEnum):
    """Whether current-only health evidence applies to an evaluation."""

    CURRENT = "CURRENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RevisionStatus(StrEnum):
    """Approved revision presentation contract."""

    NOT_AVAILABLE_FOR_CURRENT_MODE = "NOT_AVAILABLE_FOR_CURRENT_MODE"
    AVAILABLE = "AVAILABLE"
    NO_REVISION_EVIDENCE = "NO_REVISION_EVIDENCE"


class DriverAxis(StrEnum):
    """Numeric Stage 3D axes eligible for deterministic attribution."""

    GROWTH = "GROWTH"
    INFLATION_PRESSURE = "INFLATION_PRESSURE"


class EntityType(StrEnum):
    """Closed explanation evidence entity vocabulary."""

    SNAPSHOT = "SNAPSHOT"
    REGIME = "REGIME"
    MACRO_STATE = "MACRO_STATE"
    PILLAR = "PILLAR"
    SIGNAL = "SIGNAL"
    DRIVER = "DRIVER"
    READINESS = "READINESS"
    HEALTH = "HEALTH"
    CHANGE = "CHANGE"
    REVISION = "REVISION"


class ChangeType(StrEnum):
    """Deterministic comparison classifications."""

    NEW_INFORMATION = "NEW_INFORMATION"
    REVISION = "REVISION"
    STATUS_CHANGE = "STATUS_CHANGE"
    VALUE_CHANGE = "VALUE_CHANGE"
    CLASSIFICATION_CHANGE = "CLASSIFICATION_CHANGE"
    TRAJECTORY_CHANGE = "TRAJECTORY_CHANGE"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class NarrativeStatus(StrEnum):
    """Whether the final presentation used a validated LLM artifact."""

    VALIDATED_LLM = "VALIDATED_LLM"
    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"


class PersistedExplanationContext(BaseModel):
    """Locally materialized frozen-model objects accepted by the no-I/O loader."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_version: Literal["agent-explanation-context-v1"] = "agent-explanation-context-v1"
    cadence: PersistedCadence
    release_event_id: str | None = None
    regime: RegimeResult
    macro_state: MacroStateResult
    lagged_macro_state: MacroStateResult | None = None
    pillars: tuple[PillarResult, ...]
    signals: tuple[NormalizedSignalResult, ...]
    readiness: ReadinessStatus | None = None
    health: tuple[HealthSnapshot, ...] = ()
    materialized_at: datetime
    persisted_context_hash: str

    @model_validator(mode="after")
    def validate_context_alignment(self) -> PersistedExplanationContext:
        """Require same-mode, same-EOD upstream objects and unique inventories."""
        if self.materialized_at.utcoffset() is None:
            raise ValueError("materialized_at must be timezone-aware")
        if (
            self.regime.evaluation_mode is not self.macro_state.evaluation_mode
            or self.regime.evaluation_as_of != self.macro_state.evaluation_as_of
        ):
            raise ValueError("EXPLANATION_CONTEXT_EVALUATION_MISMATCH")
        if self.lagged_macro_state is not None:
            lag_hashes = {
                self.regime.growth_trajectory.lag_state_hash,
                self.regime.inflation_trajectory.lag_state_hash,
            }
            if self.lagged_macro_state.calculation_hash not in lag_hashes:
                raise ValueError("EXPLANATION_LAGGED_CONTEXT_MISMATCH")
        pillar_ids = [item.pillar_id for item in self.pillars]
        signal_ids = [item.signal_id for item in self.signals]
        if len(pillar_ids) != len(set(pillar_ids)):
            raise ValueError("DUPLICATE_PERSISTED_PILLAR")
        if len(signal_ids) != len(set(signal_ids)):
            raise ValueError("DUPLICATE_PERSISTED_SIGNAL")
        if self.regime.evaluation_mode is EvaluationMode.CURRENT:
            if self.readiness is None:
                raise ValueError("CURRENT_CONTEXT_REQUIRES_READINESS")
        elif self.readiness is not None or self.health:
            raise ValueError("PIT_CONTEXT_CANNOT_CARRY_CURRENT_FRESHNESS")
        return self

    @property
    def evaluation_as_of(self) -> date:
        """Return the canonical shared upstream evaluation date."""
        return self.regime.evaluation_as_of

    @property
    def evaluation_mode(self) -> EvaluationMode:
        """Return the canonical shared upstream evaluation mode."""
        return self.regime.evaluation_mode


class LoadedExplanationContext(BaseModel):
    """Persisted-only loader result with independently typed comparisons."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DependencyAvailability
    current: PersistedExplanationContext | None = None
    previous_release_eod_status: DependencyAvailability
    previous_release_eod: PersistedExplanationContext | None = None
    previous_month_end_status: DependencyAvailability
    previous_month_end: PersistedExplanationContext | None = None
    trajectory_context_status: DependencyAvailability


class AuthorityBindings(BaseModel):
    """Complete immutable upstream authority identities copied into a snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_registry_version: str
    source_registry_hash: str
    current_freshness_version: str | None
    current_freshness_hash: str | None
    feature_spec_version: str
    feature_spec_hash: str
    normalization_spec_version: str
    normalization_spec_hash: str
    pillar_spec_version: str
    pillar_spec_hash: str
    composite_spec_version: str
    composite_spec_hash: str
    regime_spec_version: str
    regime_spec_hash: str
    pit_materialization_version: str | None
    pit_materialization_hash: str | None
    revision_provenance_version: str | None
    revision_provenance_hash: str | None


class DeterministicDriver(BaseModel):
    """One exact Stage 3C-to-Stage 3D numeric attribution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    driver_id: str
    axis: DriverAxis
    signal_id: str
    indicator_id: str
    pillar_id: str
    contribution: Decimal
    pillar_effective_weight: Decimal
    constituent_effective_weight: Decimal
    attributed_impact: Decimal
    signal_calculation_hash: str
    pillar_calculation_hash: str
    evidence_refs: tuple[str, ...]


class DriverRankings(BaseModel):
    """Full deterministic lists plus the v1 Top-3 presentation projections."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    full_growth: tuple[DeterministicDriver, ...]
    full_inflation: tuple[DeterministicDriver, ...]
    top_positive_growth: tuple[str, ...]
    top_negative_growth: tuple[str, ...]
    top_inflationary: tuple[str, ...]
    top_disinflationary: tuple[str, ...]


class EvidenceItem(BaseModel):
    """Allow-listed fact that may support deterministic or LLM prose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    entity_type: EntityType
    entity_id: str
    status: str
    calculation_hash: str | None = None
    formatted_value: str | None = None


class ExplanationChange(BaseModel):
    """One typed deterministic delta with no missing-as-zero behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_type: EntityType
    entity_id: str
    previous_value: Decimal | None = None
    current_value: Decimal | None = None
    previous_status: str | None = None
    current_status: str | None = None
    previous_hash: str | None = None
    current_hash: str | None = None
    delta: Decimal | None = None
    change_type: ChangeType
    evidence_refs: tuple[str, ...]

    @model_validator(mode="after")
    def validate_delta(self) -> ExplanationChange:
        """Reject deltas unless both endpoints are numeric and comparable."""
        if self.delta is not None and (self.previous_value is None or self.current_value is None):
            raise ValueError("CHANGE_DELTA_REQUIRES_TWO_VALUES")
        if self.change_type is ChangeType.NOT_COMPARABLE and self.delta is not None:
            raise ValueError("NOT_COMPARABLE_CANNOT_HAVE_DELTA")
        return self


class ComparisonResult(BaseModel):
    """One canonical persisted comparison family."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DependencyAvailability
    cadence: PersistedCadence
    previous_evaluation_as_of: date | None = None
    previous_snapshot_hash: str | None = None
    changes: tuple[ExplanationChange, ...] = ()


class RevisionPresentation(BaseModel):
    """Mode-correct revision evidence presentation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RevisionStatus
    evidence_hashes: tuple[str, ...] = ()
    result_changed: bool | None = None
    note: str


class MacroExplanationSnapshot(BaseModel):
    """Canonical, hashable and provider-free input to the explanation agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_version: Literal["agent-explanation-v1"] = "agent-explanation-v1"
    snapshot_spec_hash: str
    snapshot_calculation_hash: str
    evaluation_mode: EvaluationMode
    evaluation_as_of: date
    readiness: ReadinessStatus | None
    freshness_applicability: FreshnessApplicability
    current_health: tuple[HealthSnapshot, ...]
    growth_strength: Decimal | None
    inflation_pressure: Decimal | None
    growth_condition: GrowthCondition | None
    inflation_condition: InflationCondition | None
    base_regime_id: str | None
    growth_trajectory: GrowthTrajectoryQualifier | None
    inflation_trajectory: InflationTrajectoryQualifier | None
    growth_data_confidence: Decimal
    inflation_data_confidence: Decimal
    minimum_axis_confidence: Decimal
    growth_classification_margin: Decimal | None
    inflation_classification_margin: Decimal | None
    macro_state_status: MacroStateStatus
    regime_status: RegimeStatus
    pillars: tuple[PillarResult, ...]
    drivers: DriverRankings
    contextual_diagnostics: tuple[str, ...]
    previous_release_eod: ComparisonResult
    previous_month_end: ComparisonResult
    revision: RevisionPresentation
    warnings: tuple[str, ...]
    authorities: AuthorityBindings
    upstream_calculation_hashes: tuple[str, ...]
    evidence_registry: tuple[EvidenceItem, ...]

    @model_validator(mode="after")
    def validate_snapshot(self) -> MacroExplanationSnapshot:
        """Keep canonical collections and mode-specific evidence isolated."""
        if len(self.pillars) != 9:
            raise ValueError("EXPLANATION_REQUIRES_EXACTLY_NINE_PILLARS")
        if tuple(sorted(self.pillars, key=lambda item: item.pillar_id)) != self.pillars:
            raise ValueError("EXPLANATION_PILLARS_NOT_CANONICALLY_SORTED")
        evidence_ids = [item.evidence_id for item in self.evidence_registry]
        if len(evidence_ids) != len(set(evidence_ids)) or evidence_ids != sorted(evidence_ids):
            raise ValueError("EXPLANATION_EVIDENCE_NOT_CANONICAL")
        if self.evaluation_mode is EvaluationMode.CURRENT:
            if (
                self.freshness_applicability is not FreshnessApplicability.CURRENT
                or self.readiness is None
                or self.revision.status is not RevisionStatus.NOT_AVAILABLE_FOR_CURRENT_MODE
            ):
                raise ValueError("CURRENT_EXPLANATION_MODE_CONTRACT_MISMATCH")
        elif (
            self.freshness_applicability is not FreshnessApplicability.NOT_APPLICABLE
            or self.readiness is not None
            or self.current_health
            or self.revision.status is RevisionStatus.NOT_AVAILABLE_FOR_CURRENT_MODE
        ):
            raise ValueError("PIT_EXPLANATION_MODE_CONTRACT_MISMATCH")
        return self


class DeterministicMacroExplanationReport(BaseModel):
    """Complete useful explanation artifact that does not require an LLM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_version: Literal["deterministic-macro-explanation-v1"] = (
        "deterministic-macro-explanation-v1"
    )
    snapshot_calculation_hash: str
    summary: str
    what_changed: tuple[str, ...]
    primary_drivers: tuple[str, ...]
    contextual_signals: tuple[str, ...]
    confidence_note: str
    freshness_note: str
    revision_note: str
    warnings: tuple[str, ...]
    markdown: str
    report_hash: str


class ExplanationClaim(BaseModel):
    """One validated narrative claim backed by snapshot evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    text: str
    evidence_refs: tuple[str, ...] = Field(min_length=1)


class MacroExplanationNarrative(BaseModel):
    """Provider-neutral structured non-canonical presentation artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_schema_version: Literal["macro-explanation-narrative-v1"] = (
        "macro-explanation-narrative-v1"
    )
    summary: str
    what_changed: tuple[str, ...]
    primary_drivers: tuple[str, ...]
    contextual_signals: tuple[str, ...]
    confidence_note: str
    freshness_note: str
    revision_note: str
    warnings: tuple[str, ...]
    narrative_status: NarrativeStatus
    claims: tuple[ExplanationClaim, ...]


class StructuredExplanationRequest(BaseModel):
    """Whitelisted structured request sent only by explicit LLM execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_version: str
    prompt_hash: str
    system_prompt: str
    snapshot: dict[str, Any]
    output_schema_version: Literal["macro-explanation-narrative-v1"] = (
        "macro-explanation-narrative-v1"
    )
    temperature: Decimal = Decimal(0)


class StructuredExplanationResponse(BaseModel):
    """Raw structured provider result plus non-secret run provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output: dict[str, Any]
    provider: str
    model: str | None = None
    generation_settings: dict[str, Any] = Field(default_factory=dict)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ExplanationExecutionResult(BaseModel):
    """Persistable final result with deterministic fallback semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: MacroExplanationSnapshot
    deterministic_report: DeterministicMacroExplanationReport
    narrative: MacroExplanationNarrative | None = None
    narrative_status: NarrativeStatus
    llm_error: str | None = None
    provider: str | None = None
    model: str | None = None
    generation_settings: dict[str, Any] = Field(default_factory=dict)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    prompt_version: str
    prompt_hash: str
