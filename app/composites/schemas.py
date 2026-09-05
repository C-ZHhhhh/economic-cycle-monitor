"""Typed Stage 3D macro-state specification and result contracts."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.pillars.schemas import PillarStatus
from app.source_registry.revision_schemas import RevisionLayerEffect


class StateArchitecture(StrEnum):
    """Closed Stage 3D-v1 state architecture vocabulary."""

    THREE_LAYER_STATE = "THREE_LAYER_STATE"


class GrowthAggregationMethod(StrEnum):
    """Closed Growth aggregation vocabulary."""

    EQUAL_WEIGHT_MEAN = "EQUAL_WEIGHT_MEAN"


class GrowthConfidenceMethod(StrEnum):
    """Closed Growth confidence vocabulary."""

    EXPECTED_BASE_WEIGHTED_PILLAR_CONFIDENCE = "EXPECTED_BASE_WEIGHTED_PILLAR_CONFIDENCE"


class BreadthMethod(StrEnum):
    """Closed Growth breadth vocabulary."""

    SIGNED_BREADTH = "SIGNED_BREADTH"


class DispersionMethod(StrEnum):
    """Closed Growth dispersion vocabulary."""

    POPULATION_STD = "POPULATION_STD"


class EvaluationMode(StrEnum):
    """Whether the state consumes current or date-EOD point-in-time pillars."""

    CURRENT = "CURRENT"
    PIT_DATE_EOD = "PIT_DATE_EOD"


class GrowthStatus(StrEnum):
    """Typed Growth-component states."""

    OK = "OK"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_GROWTH_COVERAGE = "INSUFFICIENT_GROWTH_COVERAGE"
    PIT_UNSAFE = "PIT_UNSAFE"
    PILLAR_ERROR = "PILLAR_ERROR"
    AS_OF_MISMATCH = "AS_OF_MISMATCH"
    SPEC_MISMATCH = "SPEC_MISMATCH"


class HistoricalCoverageState(StrEnum):
    """Availability of the expected historical macro-state universe."""

    FULL = "FULL"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class MacroStateStatus(StrEnum):
    """Fail-closed overall Stage 3D state statuses."""

    OK = "OK"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_GROWTH_COVERAGE = "INSUFFICIENT_GROWTH_COVERAGE"
    MISSING_INFLATION = "MISSING_INFLATION"
    PIT_UNSAFE = "PIT_UNSAFE"
    PILLAR_ERROR = "PILLAR_ERROR"
    AS_OF_MISMATCH = "AS_OF_MISMATCH"
    SPEC_MISMATCH = "SPEC_MISMATCH"


class MacroStateQualityFlag(StrEnum):
    """Non-destructive Stage 3D diagnostics."""

    LOW_GROWTH_COVERAGE = "LOW_GROWTH_COVERAGE"
    LOW_PILLAR_CONFIDENCE = "LOW_PILLAR_CONFIDENCE"
    CONTEXT_PARTIAL = "CONTEXT_PARTIAL"
    REVISION_CHANGED_RESULT = "REVISION_CHANGED_RESULT"
    HISTORICAL_COVERAGE_LIMITATION = "HISTORICAL_COVERAGE_LIMITATION"


class GrowthPillarWeight(BaseModel):
    """One expected Growth pillar and its immutable base weight."""

    model_config = ConfigDict(extra="forbid")

    pillar_id: str
    base_weight: Decimal = Field(gt=0)


class GrowthDefinition(BaseModel):
    """Frozen Growth-component configuration."""

    model_config = ConfigDict(extra="forbid")

    component_id: str
    unit: str
    semantic_direction: str
    aggregation_method: GrowthAggregationMethod
    minimum_growth_pillars: int = Field(gt=0)
    confidence_method: GrowthConfidenceMethod
    breadth_method: BreadthMethod
    dispersion_method: DispersionMethod
    pillars: list[GrowthPillarWeight]

    @model_validator(mode="after")
    def validate_weights(self) -> "GrowthDefinition":
        """Require unique weights that form one complete expected universe."""
        ids = [item.pillar_id for item in self.pillars]
        if len(ids) != len(set(ids)):
            raise ValueError("Growth pillar IDs must be unique")
        if self.minimum_growth_pillars > len(self.pillars):
            raise ValueError("Growth quorum cannot exceed its pillar inventory")
        if sum((item.base_weight for item in self.pillars), Decimal(0)) != Decimal(1):
            raise ValueError("Growth base weights must sum exactly to one")
        return self


class CompositeCatalog(BaseModel):
    """Hash-bound unique Stage 3D configuration authority."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    composite_spec_version: str
    required_source_registry_version: str
    required_source_registry_hash: str
    required_feature_spec_version: str
    required_normalization_spec_version: str
    required_normalization_spec_hash: str
    required_pillar_spec_version: str
    required_pillar_spec_hash: str
    architecture: StateArchitecture
    state_id: str
    growth: GrowthDefinition
    inflation_pillar_id: str
    context_pillar_ids: list[str]
    source_path: Path
    specification_hash: str


class PillarComponentSnapshot(BaseModel):
    """One Stage 3C component copied without reinterpretation."""

    model_config = ConfigDict(extra="forbid")

    pillar_id: str
    value: Decimal | None
    confidence: Decimal = Field(ge=0, le=1)
    status: PillarStatus
    semantic_direction: str
    calculation_hash: str


class GrowthComponentResult(BaseModel):
    """Auditable Growth aggregation and diagnostics."""

    model_config = ConfigDict(extra="forbid")

    component_id: str
    value: Decimal | None
    unit: str
    semantic_direction: str
    status: GrowthStatus
    confidence: Decimal = Field(ge=0, le=1)
    coverage: Decimal = Field(ge=0, le=1)
    expected_pillar_count: int = Field(gt=0)
    valid_pillar_count: int = Field(ge=0)
    aggregation_method: GrowthAggregationMethod
    pillar_ids: list[str]
    pillar_values: dict[str, Decimal | None]
    pillar_confidences: dict[str, Decimal]
    pillar_statuses: dict[str, PillarStatus]
    pillar_base_weights: dict[str, Decimal]
    pillar_effective_weights: dict[str, Decimal]
    pillar_calculation_hashes: dict[str, str]
    positive_pillar_count: int = Field(ge=0)
    negative_pillar_count: int = Field(ge=0)
    neutral_pillar_count: int = Field(ge=0)
    breadth: Decimal | None
    dispersion: Decimal | None

    @model_validator(mode="after")
    def validate_growth_result(self) -> "GrowthComponentResult":
        """Enforce conditional normalization without missing-as-zero semantics."""
        expected = set(self.pillar_ids)
        mappings = (
            self.pillar_values,
            self.pillar_confidences,
            self.pillar_statuses,
            self.pillar_base_weights,
            self.pillar_effective_weights,
            self.pillar_calculation_hashes,
        )
        if any(set(item) != expected for item in mappings):
            raise ValueError("Growth pillar audit mappings must be complete")
        if self.expected_pillar_count != len(self.pillar_ids):
            raise ValueError("Expected Growth count must match the configured inventory")
        valid = sum(value is not None for value in self.pillar_values.values())
        if self.valid_pillar_count != valid:
            raise ValueError("Valid Growth count must match non-null inputs")
        if sum(self.pillar_base_weights.values(), Decimal(0)) != Decimal(1):
            raise ValueError("Growth base weights must sum to one")
        usable_statuses = {GrowthStatus.OK, GrowthStatus.PARTIAL}
        if self.status in usable_statuses:
            if self.value is None:
                raise ValueError("Usable Growth status requires a value")
            if not Decimal(-1) < self.value < Decimal(1):
                raise ValueError("Growth value must remain strictly inside (-1, 1)")
            effective_total = sum(self.pillar_effective_weights.values(), Decimal(0))
            if abs(effective_total - Decimal(1)) > Decimal("1e-27"):
                raise ValueError("Usable Growth effective weights must sum to one")
        elif self.value is not None:
            raise ValueError("Fail-closed Growth status requires a null value")
        for pillar_id, value in self.pillar_values.items():
            effective = self.pillar_effective_weights[pillar_id]
            if value is None and effective != 0:
                raise ValueError("Missing Growth pillar must have zero effective weight")
        if self.breadth is not None and not Decimal(-1) <= self.breadth <= Decimal(1):
            raise ValueError("Growth breadth must remain in [-1, 1]")
        return self


class MacroStateResult(BaseModel):
    """Three-layer continuous Stage 3D state without regime interpretation."""

    model_config = ConfigDict(extra="forbid")

    state_id: str
    composite_spec_version: str
    composite_spec_hash: str
    evaluation_mode: EvaluationMode
    evaluation_as_of: date
    growth: GrowthComponentResult
    inflation: PillarComponentSnapshot
    context: list[PillarComponentSnapshot]
    historical_coverage_state: HistoricalCoverageState
    source_registry_hash: str
    feature_spec_version: str
    normalization_spec_version: str
    normalization_spec_hash: str
    pillar_spec_version: str
    pillar_spec_hash: str
    is_point_in_time_safe: bool
    macro_state_status: MacroStateStatus
    quality_flags: list[MacroStateQualityFlag]
    calculation_hash: str
    revision_provenance_version: str | None = None
    revision_provenance_hash: str | None = None
    revision_evidence_hashes: tuple[str, ...] = ()
    revision_effect: RevisionLayerEffect | None = None

    @model_validator(mode="after")
    def validate_state_result(self) -> "MacroStateResult":
        """Keep fail-closed state and three-layer semantics explicit."""
        if len(self.context) != 4:
            raise ValueError("Macro context must contain exactly four pillars")
        fail_closed = {
            MacroStateStatus.SPEC_MISMATCH,
            MacroStateStatus.AS_OF_MISMATCH,
            MacroStateStatus.PIT_UNSAFE,
            MacroStateStatus.PILLAR_ERROR,
        }
        if self.macro_state_status in fail_closed:
            if self.growth.value is not None or self.inflation.value is not None:
                raise ValueError("Fail-closed state cannot expose core numeric values")
            if any(item.value is not None for item in self.context):
                raise ValueError("Fail-closed state cannot expose context numeric values")
        if self.evaluation_mode is EvaluationMode.PIT_DATE_EOD:
            permitted_unsafe_statuses = {
                MacroStateStatus.SPEC_MISMATCH,
                MacroStateStatus.AS_OF_MISMATCH,
                MacroStateStatus.PIT_UNSAFE,
                MacroStateStatus.PILLAR_ERROR,
            }
            if (
                self.macro_state_status not in permitted_unsafe_statuses
                and not self.is_point_in_time_safe
            ):
                raise ValueError("Historical state must be point-in-time safe")
        elif self.is_point_in_time_safe:
            raise ValueError("Current state must not claim historical PIT safety")
        return self
