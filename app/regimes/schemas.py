"""Typed Stage 3E macro-regime specification and result contracts."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.composites.schemas import (
    EvaluationMode,
    GrowthStatus,
    MacroStateQualityFlag,
    MacroStateStatus,
    PillarComponentSnapshot,
)
from app.pillars.schemas import PillarStatus


class RegimeArchitecture(StrEnum):
    """Closed Stage 3E-v1 architecture vocabulary."""

    LEVEL_PLUS_TRAJECTORY = "LEVEL_PLUS_TRAJECTORY"


class BaseTaxonomy(StrEnum):
    """Closed base-regime taxonomy vocabulary."""

    HIERARCHICAL_3X3 = "HIERARCHICAL_3X3"


class EvaluationCadence(StrEnum):
    """Supported evaluation cadence."""

    ANY_EOD_STATELESS = "ANY_EOD_STATELESS"


class HysteresisPolicy(StrEnum):
    """Closed hysteresis policy."""

    NONE = "NONE"


class PartialStatePolicy(StrEnum):
    """How an otherwise eligible partial MacroState is classified."""

    ALLOW_IF_AXES_ELIGIBLE = "ALLOW_IF_AXES_ELIGIBLE"


class MissingAxisPolicy(StrEnum):
    """How a missing core axis affects the two-dimensional base regime."""

    FAIL_CLOSED = "FAIL_CLOSED"


class ContextPolicy(StrEnum):
    """How Stage 3D context snapshots are handled."""

    PASS_THROUGH_ONLY = "PASS_THROUGH_ONLY"


class TrajectoryMethod(StrEnum):
    """Closed trajectory calculation vocabulary."""

    DELTA_3M_REAL_TIME_PATH = "DELTA_3M_REAL_TIME_PATH"


class MarginMethod(StrEnum):
    """Closed classification-margin vocabulary."""

    DISTANCE_TO_NEAREST_BOUNDARY = "DISTANCE_TO_NEAREST_BOUNDARY"


class GrowthCondition(StrEnum):
    """Mechanical Growth-axis level condition."""

    WEAK = "WEAK"
    NEUTRAL = "NEUTRAL"
    STRONG = "STRONG"


class InflationCondition(StrEnum):
    """Mechanical Inflation Pressure-axis level condition."""

    LOW_PRESSURE = "LOW_PRESSURE"
    NEUTRAL_PRESSURE = "NEUTRAL_PRESSURE"
    HIGH_PRESSURE = "HIGH_PRESSURE"


class GrowthTrajectoryQualifier(StrEnum):
    """Growth-axis trajectory qualifier."""

    DETERIORATING = "DETERIORATING"
    STABLE = "STABLE"
    IMPROVING = "IMPROVING"


class InflationTrajectoryQualifier(StrEnum):
    """Inflation Pressure-axis trajectory qualifier without welfare judgment."""

    FALLING = "FALLING"
    STABLE = "STABLE"
    RISING = "RISING"


class TrajectoryStatus(StrEnum):
    """Independent trajectory calculation status."""

    OK = "OK"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    LOW_DATA_CONFIDENCE = "LOW_DATA_CONFIDENCE"
    PIT_UNSAFE = "PIT_UNSAFE"
    SPEC_MISMATCH = "SPEC_MISMATCH"


class RegimeStatus(StrEnum):
    """Fail-closed base-regime states."""

    OK = "OK"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_STATE = "INSUFFICIENT_STATE"
    MISSING_INFLATION = "MISSING_INFLATION"
    PIT_UNSAFE = "PIT_UNSAFE"
    AS_OF_MISMATCH = "AS_OF_MISMATCH"
    SPEC_MISMATCH = "SPEC_MISMATCH"


class RegimeQualityFlag(StrEnum):
    """Non-destructive Stage 3E diagnostics."""

    LOW_DATA_CONFIDENCE = "LOW_DATA_CONFIDENCE"
    NEAR_REGIME_BOUNDARY = "NEAR_REGIME_BOUNDARY"
    HISTORICAL_COVERAGE_LIMITATION = "HISTORICAL_COVERAGE_LIMITATION"
    REVISION_CHANGED_RESULT = "REVISION_CHANGED_RESULT"


class ExactRatio(BaseModel):
    """An exact rational value represented without decimal approximation."""

    model_config = ConfigDict(extra="forbid")

    numerator: int = Field(ge=0)
    denominator: int = Field(gt=0)

    @property
    def value(self) -> Decimal:
        """Return the exact ratio in the active Decimal context."""
        return Decimal(self.numerator) / Decimal(self.denominator)

    def accepts(self, candidate: Decimal) -> bool:
        """Compare to the rational boundary without division or approximation."""
        candidate_numerator, candidate_denominator = candidate.as_integer_ratio()
        return candidate_numerator * self.denominator >= self.numerator * candidate_denominator


class AxisDefinition(BaseModel):
    """One axis's level thresholds and evidence gate."""

    model_config = ConfigDict(extra="forbid")

    lower_threshold: Decimal
    upper_threshold: Decimal
    minimum_confidence: ExactRatio

    @model_validator(mode="after")
    def validate_thresholds(self) -> "AxisDefinition":
        """Require an ordered band inside the Stage 3D internal scale."""
        if not Decimal(-1) < self.lower_threshold < self.upper_threshold < Decimal(1):
            raise ValueError("Axis thresholds must be ordered inside (-1, 1)")
        if not Decimal(0) <= self.minimum_confidence.value <= Decimal(1):
            raise ValueError("Minimum confidence must be in [0, 1]")
        return self


class TrajectoryDefinition(BaseModel):
    """Frozen real-time-path trajectory definition."""

    model_config = ConfigDict(extra="forbid")

    method: TrajectoryMethod
    horizon_months: int = Field(gt=0)
    lower_threshold: Decimal
    upper_threshold: Decimal

    @model_validator(mode="after")
    def validate_thresholds(self) -> "TrajectoryDefinition":
        """Require an ordered stable band."""
        if self.lower_threshold >= self.upper_threshold:
            raise ValueError("Trajectory thresholds must be ordered")
        return self


class MarginDefinition(BaseModel):
    """Classification-margin method and warning boundary."""

    model_config = ConfigDict(extra="forbid")

    method: MarginMethod
    warning_threshold: Decimal = Field(ge=0)


class BaseRegimeDefinition(BaseModel):
    """One stable mechanical Growth-by-Inflation regime identity."""

    model_config = ConfigDict(extra="forbid")

    regime_id: str
    display_name: str
    growth_condition: GrowthCondition
    inflation_condition: InflationCondition


class RegimeCatalog(BaseModel):
    """Hash-bound unique Stage 3E configuration authority."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    regime_spec_version: str
    required_stage3d_spec_version: str
    required_stage3d_spec_hash: str
    required_source_registry_version: str
    required_source_registry_hash: str
    required_feature_spec_version: str
    required_normalization_spec_version: str
    required_normalization_spec_hash: str
    required_pillar_spec_version: str
    required_pillar_spec_hash: str
    architecture: RegimeArchitecture
    base_taxonomy: BaseTaxonomy
    evaluation_cadence: EvaluationCadence
    hysteresis_policy: HysteresisPolicy
    partial_state_policy: PartialStatePolicy
    missing_axis_policy: MissingAxisPolicy
    context_policy: ContextPolicy
    growth: AxisDefinition
    inflation: AxisDefinition
    trajectory: TrajectoryDefinition
    classification_margin: MarginDefinition
    base_regimes: list[BaseRegimeDefinition]
    source_path: Path
    specification_hash: str

    def by_conditions(
        self,
    ) -> dict[tuple[GrowthCondition, InflationCondition], BaseRegimeDefinition]:
        """Index the complete mechanical taxonomy by its two conditions."""
        return {
            (item.growth_condition, item.inflation_condition): item for item in self.base_regimes
        }


class AxisTrajectoryResult(BaseModel):
    """Auditable current-versus-lagged real-time-path trajectory."""

    model_config = ConfigDict(extra="forbid")

    horizon_months: int = Field(gt=0)
    current_as_of: date
    lag_as_of: date
    current_value: Decimal | None
    lag_value: Decimal | None
    current_confidence: Decimal = Field(ge=0, le=1)
    lag_confidence: Decimal | None = Field(default=None, ge=0, le=1)
    delta: Decimal | None
    qualifier: GrowthTrajectoryQualifier | InflationTrajectoryQualifier | None
    status: TrajectoryStatus
    current_state_hash: str
    lag_state_hash: str | None

    @model_validator(mode="after")
    def validate_trajectory(self) -> "AxisTrajectoryResult":
        """Require numeric provenance only for successful trajectories."""
        if self.status is TrajectoryStatus.OK:
            if (
                self.current_value is None
                or self.lag_value is None
                or self.lag_confidence is None
                or self.delta is None
                or self.qualifier is None
                or self.lag_state_hash is None
            ):
                raise ValueError("Successful trajectory requires complete endpoint provenance")
        elif self.delta is not None or self.qualifier is not None:
            raise ValueError("Failed trajectory cannot expose a delta or qualifier")
        return self


class RegimeResult(BaseModel):
    """Stage 3E level classification with independent trajectory qualifiers."""

    model_config = ConfigDict(extra="forbid")

    regime_spec_version: str
    regime_spec_hash: str
    evaluation_mode: EvaluationMode
    evaluation_as_of: date
    macro_state_id: str
    macro_state_calculation_hash: str
    stage3d_spec_version: str
    stage3d_spec_hash: str
    growth_strength: Decimal | None
    growth_data_confidence: Decimal = Field(ge=0, le=1)
    growth_status: GrowthStatus
    growth_condition: GrowthCondition | None
    growth_classification_margin: Decimal | None = Field(default=None, ge=0)
    inflation_pressure: Decimal | None
    inflation_data_confidence: Decimal = Field(ge=0, le=1)
    inflation_status: PillarStatus
    inflation_condition: InflationCondition | None
    inflation_classification_margin: Decimal | None = Field(default=None, ge=0)
    minimum_axis_data_confidence: Decimal = Field(ge=0, le=1)
    base_regime_id: str | None
    base_regime_display_name: str | None
    growth_trajectory: AxisTrajectoryResult
    inflation_trajectory: AxisTrajectoryResult
    context: list[PillarComponentSnapshot]
    macro_state_status: MacroStateStatus
    is_point_in_time_safe: bool
    regime_status: RegimeStatus
    quality_flags: list[RegimeQualityFlag]
    source_registry_hash: str
    feature_spec_version: str
    normalization_spec_version: str
    normalization_spec_hash: str
    pillar_spec_version: str
    pillar_spec_hash: str
    calculation_hash: str

    @model_validator(mode="after")
    def validate_result(self) -> "RegimeResult":
        """Keep fail-closed base classification and pass-through context explicit."""
        if len(self.context) != 4:
            raise ValueError("Regime context must preserve exactly four Stage 3D snapshots")
        base_is_complete = self.base_regime_id is not None
        if base_is_complete != (self.base_regime_display_name is not None):
            raise ValueError("Base regime ID and display name must be present together")
        if base_is_complete and (self.growth_condition is None or self.inflation_condition is None):
            raise ValueError("Complete base regime requires both axis conditions")
        if self.regime_status not in {RegimeStatus.OK, RegimeStatus.PARTIAL} and base_is_complete:
            raise ValueError("Fail-closed regime status cannot expose a base regime")
        return self


UPSTREAM_HISTORICAL_FLAG_MAP: dict[MacroStateQualityFlag, RegimeQualityFlag] = {
    MacroStateQualityFlag.HISTORICAL_COVERAGE_LIMITATION: (
        RegimeQualityFlag.HISTORICAL_COVERAGE_LIMITATION
    ),
    MacroStateQualityFlag.REVISION_CHANGED_RESULT: RegimeQualityFlag.REVISION_CHANGED_RESULT,
}
