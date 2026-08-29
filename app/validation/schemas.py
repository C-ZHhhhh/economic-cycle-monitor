"""Typed contracts for Stage 3F historical validation."""

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValidationPhase(StrEnum):
    """Authorized execution phase."""

    PILOT = "PILOT"
    FULL = "FULL"


class UniverseMode(StrEnum):
    """Separate historical-universe interpretations."""

    FULL_FIXED_UNIVERSE = "FULL_FIXED_UNIVERSE"
    DYNAMIC_REAL_TIME_UNIVERSE = "DYNAMIC_REAL_TIME_UNIVERSE"


class ValidationCadence(StrEnum):
    """Independent canonical replay schedules."""

    RELEASE_EVENT_EOD = "RELEASE_EVENT_EOD"
    MONTH_END_EOD = "MONTH_END_EOD"


class ValidationStatus(StrEnum):
    """Stage 3F validation outcomes without overstating a pilot."""

    PILOT_COMPLETE = "PILOT_COMPLETE"
    PASS = "PASS"
    PASS_WITH_HISTORICAL_LIMITATIONS = "PASS_WITH_HISTORICAL_LIMITATIONS"
    FAIL_FUTURE_LEAKAGE = "FAIL_FUTURE_LEAKAGE"
    FAIL_AUTHORITY_DRIFT = "FAIL_AUTHORITY_DRIFT"
    FAIL_NONDETERMINISM = "FAIL_NONDETERMINISM"
    FAIL_PIT_SAFETY = "FAIL_PIT_SAFETY"
    FAIL_INSUFFICIENT_VALIDATION_DATA = "FAIL_INSUFFICIENT_VALIDATION_DATA"
    PARTIAL_RUN = "PARTIAL_RUN"


class StopReason(StrEnum):
    """Bounded-run stop causes."""

    PLANNER_COMPLETE = "PLANNER_COMPLETE"
    REQUEST_BUDGET = "REQUEST_BUDGET"
    STORAGE_BUDGET = "STORAGE_BUDGET"
    WALL_CLOCK_BUDGET = "WALL_CLOCK_BUDGET"
    HARD_QUALITY_FAILURE = "HARD_QUALITY_FAILURE"


class CoverageBoundary(StrEnum):
    """Ordered production-layer date-EOD boundaries."""

    RAW_PIT_START = "RAW_PIT_START"
    FEATURE_PIT_START = "FEATURE_PIT_START"
    SIGNAL_PIT_START = "SIGNAL_PIT_START"
    PILLAR_PIT_START = "PILLAR_PIT_START"
    MACRO_STATE_PIT_START = "MACRO_STATE_PIT_START"
    REGIME_LEVEL_PIT_START = "REGIME_LEVEL_PIT_START"
    REGIME_TRAJECTORY_PIT_START = "REGIME_TRAJECTORY_PIT_START"


class BoundaryCandidateStatus(StrEnum):
    """One candidate date's evidence state."""

    SUCCESS = "SUCCESS"
    OUTSIDE_COVERAGE = "OUTSIDE_COVERAGE"
    NO_VINTAGE = "NO_VINTAGE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    ERROR = "ERROR"
    NOT_EVALUATED = "NOT_EVALUATED"


class ExactRatio(BaseModel):
    """An exact rational threshold."""

    model_config = ConfigDict(extra="forbid")

    numerator: int = Field(ge=0)
    denominator: int = Field(gt=0)

    @property
    def value(self) -> Decimal:
        """Return the Decimal value in the active context."""
        return Decimal(self.numerator) / Decimal(self.denominator)

    def accepts(self, candidate: Decimal) -> bool:
        """Compare exactly without a rounded division boundary."""
        candidate_numerator, candidate_denominator = candidate.as_integer_ratio()
        return candidate_numerator * self.denominator >= self.numerator * candidate_denominator


class AuthorityBindings(BaseModel):
    """Production authorities required by the validation authority."""

    model_config = ConfigDict(extra="forbid")

    source_registry_version: str
    source_registry_hash: str
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

    def ordered_hashes(self) -> list[str]:
        """Return hashes in frozen Stage 2-through-3E order."""
        return [
            self.source_registry_hash,
            self.feature_spec_hash,
            self.normalization_spec_hash,
            self.pillar_spec_hash,
            self.composite_spec_hash,
            self.regime_spec_hash,
        ]


class CadenceDefinition(BaseModel):
    """Canonical and benchmark scheduling rules."""

    model_config = ConfigDict(extra="forbid")

    primary: ValidationCadence
    benchmark: ValidationCadence
    pit_granularity: str
    same_day_policy: str
    intraday_reconstruction: bool


class MaterializationDefinition(BaseModel):
    """Bounded, cache-aware historical materialization strategy."""

    model_config = ConfigDict(extra="forbid")

    strategy: str
    reuse_exact_local_snapshots: bool
    fetch_only_exact_snapshot_misses: bool
    indicator_major_discovery: bool
    date_major_production_replay: bool
    sensitivity_refetch_allowed: bool


class PlannerDefinition(BaseModel):
    """Pilot-window selection constraints."""

    model_config = ConfigDict(extra="forbid")

    discovery_lookback_days: int = Field(gt=0)
    minimum_window_days: int = Field(gt=0)
    maximum_window_days: int = Field(gt=0)
    maximum_release_events: int = Field(gt=0)
    require_frequencies: list[str]
    require_primary_source_families: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_window(self) -> "PlannerDefinition":
        """Require an ordered bounded pilot window."""
        if self.minimum_window_days > self.maximum_window_days:
            raise ValueError("Pilot minimum window cannot exceed maximum window")
        if set(self.require_frequencies) != {"weekly", "monthly", "quarterly"}:
            raise ValueError("Pilot must cover weekly, monthly, and quarterly frequencies")
        return self


class BudgetLimits(BaseModel):
    """Hard resource limits for one execution phase."""

    model_config = ConfigDict(extra="forbid")

    external_alfred_http_requests: int = Field(gt=0)
    incremental_storage_bytes: int = Field(gt=0)
    wall_clock_seconds: int = Field(gt=0)
    alfred_concurrency: int = Field(default=1, gt=0)
    global_http_concurrency: int = Field(default=4, gt=0)
    per_source_http_concurrency: int = Field(default=2, gt=0)

    @model_validator(mode="after")
    def validate_concurrency(self) -> "BudgetLimits":
        """Preserve the approved single-ALFRED-request execution model."""
        if self.alfred_concurrency != 1:
            raise ValueError("ALFRED concurrency must remain one")
        if self.per_source_http_concurrency > self.global_http_concurrency:
            raise ValueError("Per-source concurrency cannot exceed global concurrency")
        return self


class FullRunBudget(BudgetLimits):
    """Provisional, explicitly unapproved full-run cap."""

    approved: bool


class BudgetDefinition(BaseModel):
    """Pilot limits and separate provisional full-run limits."""

    model_config = ConfigDict(extra="forbid")

    pilot: BudgetLimits
    provisional_full_run: FullRunBudget


class ProductionSensitivity(BaseModel):
    """Frozen Stage 3E production values represented in Stage 3F."""

    model_config = ConfigDict(extra="forbid")

    neutral_band: Decimal
    trajectory_horizon_months: int
    trajectory_stable_band: Decimal
    confidence_gate: ExactRatio


class SensitivityDefinition(BaseModel):
    """Approved offline counterfactual grids."""

    model_config = ConfigDict(extra="forbid")

    neutral_bands: list[Decimal]
    trajectory_horizons_months: list[int]
    trajectory_stable_bands: list[Decimal]
    confidence_gates: list[ExactRatio]
    production: ProductionSensitivity
    deferred: list[str]


class TrajectoryDefinition(BaseModel):
    """Production real-time path trajectory contract."""

    model_config = ConfigDict(extra="forbid")

    production_horizon_months: int = Field(gt=0)
    path: str
    lag_endpoint_must_be_schedule_event: bool


class HysteresisResearchDefinition(BaseModel):
    """Non-production hysteresis research scope."""

    model_config = ConfigDict(extra="forbid")

    policies: list[str]
    production_writeback: bool
    automatic_ranking: bool


class RevisionSensitivityDefinition(BaseModel):
    """Limited representative revision checks."""

    model_config = ConfigDict(extra="forbid")

    mode: str
    indicators: list[str]
    build_alternate_latest_revised_history: bool


class ArtifactFormats(BaseModel):
    """Approved dependency-light artifact formats."""

    model_config = ConfigDict(extra="forbid")

    manifest: str
    status: str
    sensitivity: str
    provenance: str
    tabular: str
    report: str
    parquet_enabled: bool


class ValidationCatalog(BaseModel):
    """Unique Stage 3F validation authority."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    validation_spec_version: str
    validation_phase: ValidationPhase
    authorities: AuthorityBindings
    universe_modes: list[UniverseMode]
    cadence: CadenceDefinition
    materialization: MaterializationDefinition
    coverage_boundaries: list[CoverageBoundary]
    pilot_planner: PlannerDefinition
    budgets: BudgetDefinition
    trajectory: TrajectoryDefinition
    sensitivity: SensitivityDefinition
    hysteresis_research: HysteresisResearchDefinition
    revision_sensitivity: RevisionSensitivityDefinition
    quality_hard_gates: list[str]
    checkpoint_path: Path
    artifact_root: Path
    artifact_formats: ArtifactFormats
    source_path: Path
    specification_hash: str


class SemanticInformationSet(BaseModel):
    """Provider-free semantic identity for one indicator at one EOD."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    as_of: date
    observation_count: int = Field(ge=0)
    latest_observation_date: date | None
    semantic_hash: str


class ReleaseCandidate(BaseModel):
    """One indicator-level candidate information-change date."""

    model_config = ConfigDict(extra="forbid")

    event_date: date
    indicator_id: str
    frequency: str
    source_family: str


class ReleaseEvent(BaseModel):
    """One coalesced date-EOD semantic information-set change."""

    model_config = ConfigDict(extra="forbid")

    event_date: date
    changed_indicator_ids: list[str]
    previous_information_set_hashes: dict[str, str | None]
    new_information_set_hashes: dict[str, str]
    event_hash: str


class EvaluationSchedule(BaseModel):
    """Deterministic dates for one cadence."""

    model_config = ConfigDict(extra="forbid")

    cadence: ValidationCadence
    dates: list[date]
    schedule_hash: str


class MaterializationUnit(BaseModel):
    """One resumable logical PIT unit, distinct from HTTP attempts."""

    model_config = ConfigDict(extra="forbid")

    unit_id: str
    unit_type: str
    indicator_id: str | None = None
    as_of: date | None = None
    cadence: ValidationCadence | None = None


class MaterializationPlan(BaseModel):
    """Bounded pilot plan with current and trajectory endpoints."""

    model_config = ConfigDict(extra="forbid")

    phase: ValidationPhase
    window_start: date
    window_end: date
    release_schedule: EvaluationSchedule
    month_end_schedule: EvaluationSchedule
    lag_endpoint_dates: list[date]
    units: list[MaterializationUnit]
    planned_logical_pit_units: int
    plan_hash: str


class BudgetSnapshot(BaseModel):
    """Current counters for a bounded execution."""

    model_config = ConfigDict(extra="forbid")

    logical_pit_units: int = Field(ge=0)
    completed_logical_units: int = Field(ge=0)
    external_alfred_http_requests: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    cache_reuse_count: int = Field(ge=0)
    incremental_storage_bytes: int = Field(ge=0)
    elapsed_wall_seconds: Decimal = Field(ge=0)


class Checkpoint(BaseModel):
    """Gitignored resumable state bound to authorities and schedule."""

    model_config = ConfigDict(extra="forbid")

    validation_spec_hash: str
    production_authority_hashes: list[str]
    schedule_hash: str
    universe_mode: UniverseMode
    completed_logical_units: list[str]
    failed_units: dict[str, str]
    counters: BudgetSnapshot
    artifact_hashes: dict[str, str]
    stop_reason: StopReason | None
    checkpoint_timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CoverageBoundaryEvidence(BaseModel):
    """Proof that a boundary is the first eligible candidate tested."""

    model_config = ConfigDict(extra="forbid")

    boundary: CoverageBoundary
    first_successful_candidate_date: date
    previous_candidate_date: date | None
    previous_candidate_status: BoundaryCandidateStatus | None
    source_evidence: list[str]
    production_authority_hashes: list[str]
    calculation_hashes: list[str]


class CoverageSnapshot(BaseModel):
    """Layer-by-layer coverage at one date-EOD information set."""

    model_config = ConfigDict(extra="forbid")

    evaluation_as_of: date
    raw_available_count: int = Field(ge=0)
    feature_available_count: int = Field(ge=0)
    signal_available_count: int = Field(ge=0)
    pillar_available_count: int = Field(ge=0)
    macro_state_available: bool
    regime_level_available: bool
    regime_trajectory_available: bool
    primary_expected_count: int = Field(default=33, gt=0)
    pillar_expected_count: int = Field(default=9, gt=0)
    calculation_hashes: list[str]
    source_evidence: list[str]


class RegimeHistoryRow(BaseModel):
    """Semantic production result used by transition and replay analysis."""

    model_config = ConfigDict(extra="forbid")

    universe_mode: UniverseMode
    cadence: ValidationCadence
    evaluation_as_of: date
    growth_strength: Decimal | None
    inflation_pressure: Decimal | None
    growth_trajectory_delta: Decimal | None
    inflation_trajectory_delta: Decimal | None
    base_regime_id: str | None
    growth_condition: str | None
    inflation_condition: str | None
    growth_trajectory: str | None
    inflation_trajectory: str | None
    minimum_axis_data_confidence: Decimal
    regime_status: str
    is_point_in_time_safe: bool
    regime_calculation_hash: str
    macro_state_calculation_hash: str
    semantic_validation_status: str


class QualityEvidence(BaseModel):
    """First hard-gate evidence without credentials or raw payloads."""

    model_config = ConfigDict(extra="forbid")

    gate: str
    evaluation_as_of: date | None
    detail: str
    evidence_hash: str


class PilotSummary(BaseModel):
    """Compact bounded-pilot outcome."""

    model_config = ConfigDict(extra="forbid")

    validation_phase: ValidationPhase
    validation_status: ValidationStatus
    stop_reason: StopReason
    pilot_window_start: date
    pilot_window_end: date
    release_event_evaluations: int = Field(ge=0)
    month_end_evaluations: int = Field(ge=0)
    same_day_releases_coalesced: int = Field(ge=0)
    counters: BudgetSnapshot
    failed_logical_units: int = Field(default=0, ge=0)
    failed_unit_status_counts: dict[str, int] = Field(default_factory=dict)
    future_leakage_count: int = Field(ge=0)
    same_as_of_violation_count: int = Field(ge=0)
    pit_unsafe_classified_count: int = Field(ge=0)
    deterministic_replay_match: bool
    authority_drift_count: int = Field(ge=0)
    history_run_hashes: dict[str, str]
    boundaries: list[CoverageBoundaryEvidence]
    unresolved_boundaries: list[CoverageBoundary]
    artifact_hashes: dict[str, str]

    @model_validator(mode="after")
    def prevent_pilot_from_claiming_full_pass(self) -> "PilotSummary":
        """An approved pilot can never assert completion of full Stage 3F."""
        if self.validation_phase is ValidationPhase.PILOT and self.validation_status in {
            ValidationStatus.PASS,
            ValidationStatus.PASS_WITH_HISTORICAL_LIMITATIONS,
        }:
            raise ValueError("PILOT_CANNOT_REPORT_FULL_PASS")
        return self
