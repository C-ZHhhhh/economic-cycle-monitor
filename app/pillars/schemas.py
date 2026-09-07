"""Typed Stage 3C pillar specification and result contracts."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.source_registry.revision_schemas import RevisionLayerEffect


class PillarRole(StrEnum):
    """Whether a pillar is eligible for a future core composite."""

    CORE = "CORE"
    CONTEXTUAL = "CONTEXTUAL"


class AggregationMethod(StrEnum):
    """Closed Stage 3C-v1 aggregation vocabulary."""

    DIRECT_EQUAL_INDICATOR = "DIRECT_EQUAL_INDICATOR"


class ContributionMapping(StrEnum):
    """Closed Stage 3C-v1 contribution mapping vocabulary."""

    TANH_SATURATION = "TANH_SATURATION"


class SupportingUsage(StrEnum):
    """Permitted non-numeric supporting-signal uses."""

    CONFIRMATION = "CONFIRMATION"
    DIVERGENCE = "DIVERGENCE"
    MOMENTUM_CONTEXT = "MOMENTUM_CONTEXT"
    NOT_USED_V1 = "NOT_USED_V1"


class SupportingZone(StrEnum):
    """Directional zone for a valid Stage 3B signal."""

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"


class SupportingConfirmationStatus(StrEnum):
    """Status-only comparison between one primary and its supporting signals."""

    CONFIRMS_PRIMARY = "CONFIRMS_PRIMARY"
    DIVERGES_FROM_PRIMARY = "DIVERGES_FROM_PRIMARY"
    MIXED = "MIXED"
    INSUFFICIENT = "INSUFFICIENT"


class PillarStatus(StrEnum):
    """Fail-closed Stage 3C pillar states."""

    OK = "OK"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"
    NO_VALID_SIGNALS = "NO_VALID_SIGNALS"
    PIT_UNSAFE = "PIT_UNSAFE"
    SIGNAL_ERROR = "SIGNAL_ERROR"
    SPEC_MISMATCH = "SPEC_MISMATCH"


class PillarQualityFlag(StrEnum):
    """Non-destructive Stage 3C diagnostics."""

    DEGRADED_INPUT = "DEGRADED_INPUT"
    FALLBACK_INPUT = "FALLBACK_INPUT"
    EXTREME_CONTRIBUTION = "EXTREME_CONTRIBUTION"
    DIVERGENT_SUPPORTING_SIGNALS = "DIVERGENT_SUPPORTING_SIGNALS"
    LOW_COVERAGE = "LOW_COVERAGE"
    CONCENTRATION_WARNING = "CONCENTRATION_WARNING"
    REVISION_CHANGED_RESULT = "REVISION_CHANGED_RESULT"


class ContributionMappingParameters(BaseModel):
    """Immutable tanh saturation parameters."""

    model_config = ConfigDict(extra="forbid")

    amplitude: Decimal = Field(gt=0)
    scale: Decimal = Field(gt=0)


class CoveragePolicy(BaseModel):
    """Exact rational minimum-coverage policy."""

    model_config = ConfigDict(extra="forbid")

    minimum_numerator: int = Field(gt=0)
    minimum_denominator: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_fraction(self) -> "CoveragePolicy":
        """Require a proper fraction no greater than one."""
        if self.minimum_numerator > self.minimum_denominator:
            raise ValueError("Coverage threshold cannot exceed one")
        return self

    @property
    def minimum(self) -> Decimal:
        """Return the exact Decimal threshold."""
        return Decimal(self.minimum_numerator) / Decimal(self.minimum_denominator)


class SupportingSignalDefinition(BaseModel):
    """One complete non-numeric supporting binding."""

    model_config = ConfigDict(extra="forbid")

    signal_id: str
    related_pillar_id: str
    redundancy_group: str
    intended_usage: SupportingUsage


class PillarDefinition(BaseModel):
    """One approved pillar and its exact constituent inventory."""

    model_config = ConfigDict(extra="forbid")

    pillar_id: str
    display_name: str
    description: str
    pillar_role: PillarRole
    core_composite_eligible: bool
    semantic_direction: str
    primary_signal_ids: list[str]
    supporting_signals: list[SupportingSignalDefinition]
    concentration_warning: bool = False

    @model_validator(mode="after")
    def validate_role(self) -> "PillarDefinition":
        """Prevent contextual pillars from becoming implicit core inputs."""
        if self.pillar_role is PillarRole.CONTEXTUAL and self.core_composite_eligible:
            raise ValueError("Contextual pillars cannot be core-composite eligible")
        return self


class PillarCatalog(BaseModel):
    """Hash-bound unique Stage 3C configuration authority."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    pillar_spec_version: str
    required_source_registry_version: str
    required_source_registry_hash: str
    required_feature_spec_version: str
    required_normalization_spec_version: str
    required_normalization_spec_hash: str
    expected_signal_count: int
    expected_primary_count: int
    expected_supporting_count: int
    aggregation_method: AggregationMethod
    contribution_mapping: ContributionMapping
    contribution_mapping_parameters: ContributionMappingParameters
    coverage_policy: CoveragePolicy
    confidence_method: str
    latest_selection_method: str
    latest_known_search_months: int = Field(gt=0)
    pillars: list[PillarDefinition]
    source_path: Path
    specification_hash: str

    def by_pillar(self) -> dict[str, PillarDefinition]:
        """Return definitions keyed by stable pillar ID."""
        return {item.pillar_id: item for item in self.pillars}


class SupportingConfirmation(BaseModel):
    """Auditable status-only redundancy-group diagnostic."""

    model_config = ConfigDict(extra="forbid")

    redundancy_group: str
    primary_signal_id: str
    primary_zone: SupportingZone | None
    valid_supporting_count: int
    same_direction_count: int
    opposite_direction_count: int
    neutral_count: int
    status: SupportingConfirmationStatus
    supporting_signal_ids: list[str]


class ConfidenceComponents(BaseModel):
    """Visible diagnostics that do not alter Stage 3C-v1 confidence."""

    model_config = ConfigDict(extra="forbid")

    coverage: Decimal
    source_health_coverage: Decimal | None
    freshness_coverage: Decimal | None
    agreement_ratio: Decimal | None
    revision_state: str


class ConcentrationMetadata(BaseModel):
    """Static/runtime concentration audit without weight adjustment."""

    model_config = ConfigDict(extra="forbid")

    primary_count: int
    axis_count: int
    largest_axis_share: Decimal
    largest_redundancy_group_share: Decimal


class PillarResult(BaseModel):
    """Auditable compute-on-demand Stage 3C pillar result."""

    model_config = ConfigDict(extra="forbid")

    pillar_id: str
    display_name: str
    description: str
    pillar_role: PillarRole
    core_composite_eligible: bool
    pillar_spec_version: str
    pillar_spec_hash: str
    evaluation_as_of: date
    value: Decimal | None
    unit: str
    semantic_direction: str
    confidence: Decimal
    confidence_components: ConfidenceComponents
    aggregation_method: AggregationMethod
    contribution_mapping: ContributionMapping
    contribution_mapping_parameters: ContributionMappingParameters
    expected_constituent_count: int
    valid_constituent_count: int
    expected_weight: Decimal
    available_weight: Decimal
    coverage: Decimal
    constituent_signal_ids: list[str]
    constituent_observation_dates: dict[str, date | None]
    constituent_normalized_values: dict[str, Decimal | None]
    constituent_directed_values: dict[str, Decimal | None]
    constituent_pre_map_values: dict[str, Decimal | None]
    constituent_contributions: dict[str, Decimal | None]
    constituent_weights: dict[str, Decimal]
    constituent_statuses: dict[str, str]
    constituent_quality_flags: dict[str, list[str]]
    constituent_calculation_hashes: dict[str, str | None]
    supporting_confirmation: list[SupportingConfirmation]
    concentration: ConcentrationMetadata
    source_registry_hash: str
    feature_spec_version: str
    normalization_spec_version: str
    normalization_spec_hash: str
    is_point_in_time_safe: bool
    pillar_status: PillarStatus
    quality_flags: list[PillarQualityFlag]
    calculation_hash: str
    revision_provenance_version: str | None = None
    revision_provenance_hash: str | None = None
    revision_evidence_hashes: tuple[str, ...] = ()
    revision_effect: RevisionLayerEffect | None = None

    @model_validator(mode="after")
    def validate_result_contract(self) -> "PillarResult":
        """Keep score, coverage, confidence, and status semantics fail closed."""
        value_statuses = {PillarStatus.OK, PillarStatus.PARTIAL}
        if self.pillar_status in value_statuses and self.value is None:
            raise ValueError("Usable pillar status requires a value")
        if self.pillar_status not in value_statuses and self.value is not None:
            raise ValueError("Fail-closed pillar status requires a null value")
        if self.value is not None and not Decimal(-1) < self.value < Decimal(1):
            raise ValueError("Pillar value must remain strictly inside (-1, 1)")
        if self.confidence != self.coverage:
            raise ValueError("Stage 3C-v1 confidence must equal coverage")
        if self.expected_weight != Decimal(self.expected_constituent_count):
            raise ValueError("Expected weight must equal expected primary count")
        if self.available_weight != Decimal(self.valid_constituent_count):
            raise ValueError("Available weight must equal valid primary count")
        if set(self.constituent_signal_ids) != set(self.constituent_weights):
            raise ValueError("Constituent weight inventory is incomplete")
        return self
