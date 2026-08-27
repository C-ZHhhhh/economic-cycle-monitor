"""Typed Stage 3B signal specification and result contracts."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EconomicAxis(StrEnum):
    """Approved economic semantics without macro good/bad interpretation."""

    REAL_ACTIVITY_STRENGTH = "REAL_ACTIVITY_STRENGTH"
    CONSUMER_ACTIVITY_STRENGTH = "CONSUMER_ACTIVITY_STRENGTH"
    HOUSEHOLD_INCOME_STRENGTH = "HOUSEHOLD_INCOME_STRENGTH"
    LABOR_STRENGTH = "LABOR_STRENGTH"
    LABOR_TIGHTNESS = "LABOR_TIGHTNESS"
    LABOR_PARTICIPATION = "LABOR_PARTICIPATION"
    INFLATION_PRESSURE = "INFLATION_PRESSURE"
    WAGE_PRESSURE = "WAGE_PRESSURE"
    MANUFACTURING_STRENGTH = "MANUFACTURING_STRENGTH"
    HOUSING_STRENGTH = "HOUSING_STRENGTH"
    BUSINESS_INVESTMENT_STRENGTH = "BUSINESS_INVESTMENT_STRENGTH"
    CREDIT_USAGE_PRESSURE = "CREDIT_USAGE_PRESSURE"
    HOUSEHOLD_FINANCIAL_CUSHION = "HOUSEHOLD_FINANCIAL_CUSHION"
    INVENTORY_ABSORPTION_STRENGTH = "INVENTORY_ABSORPTION_STRENGTH"
    INVENTORY_ACCUMULATION_PRESSURE = "INVENTORY_ACCUMULATION_PRESSURE"


class Direction(StrEnum):
    """How a normalized feature maps to its economic axis."""

    POSITIVE = "POSITIVE"
    INVERSE = "INVERSE"
    NEUTRAL_CONTEXT_ONLY = "NEUTRAL_CONTEXT_ONLY"


class NormalizationMethod(StrEnum):
    """Closed Stage 3B-v1 production normalizer vocabulary."""

    ROLLING_ZSCORE = "ROLLING_ZSCORE"


class SignalStatus(StrEnum):
    """Fail-closed states for one normalized signal date."""

    OK = "OK"
    NORMALIZATION_INSUFFICIENT_HISTORY = "NORMALIZATION_INSUFFICIENT_HISTORY"
    MISSING_FEATURE = "MISSING_FEATURE"
    FEATURE_ERROR = "FEATURE_ERROR"
    ZERO_DISPERSION = "ZERO_DISPERSION"
    NONFINITE_INPUT = "NONFINITE_INPUT"
    NONFINITE_RESULT = "NONFINITE_RESULT"
    PIT_UNSAFE = "PIT_UNSAFE"
    REGISTRY_HASH_MISMATCH = "REGISTRY_HASH_MISMATCH"
    FEATURE_SPEC_MISMATCH = "FEATURE_SPEC_MISMATCH"
    NORMALIZATION_SPEC_MISMATCH = "NORMALIZATION_SPEC_MISMATCH"
    UNSUPPORTED_NORMALIZER = "UNSUPPORTED_NORMALIZER"
    UNSUPPORTED_DIRECTION = "UNSUPPORTED_DIRECTION"


class SignalQualityFlag(StrEnum):
    """Non-destructive context attached to a normalized signal."""

    FALLBACK_INPUT = "FALLBACK_INPUT"
    STALE_INPUT = "STALE_INPUT"
    DEGRADED_INPUT = "DEGRADED_INPUT"
    RELEASE_DATE_UNAVAILABLE = "RELEASE_DATE_UNAVAILABLE"
    REVISION_CHANGED_RESULT = "REVISION_CHANGED_RESULT"
    MIXED_INPUT_SOURCES = "MIXED_INPUT_SOURCES"
    PARTIAL_WINDOW = "PARTIAL_WINDOW"
    EXTREME_VALUE = "EXTREME_VALUE"
    LOW_DISPERSION_RELIABILITY = "LOW_DISPERSION_RELIABILITY"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    REVISION_CHANGED_DISTRIBUTION = "REVISION_CHANGED_DISTRIBUTION"


class SignalDefinition(BaseModel):
    """One approved feature-to-axis normalization mapping."""

    model_config = ConfigDict(extra="forbid")

    signal_id: str
    feature_id: str
    indicator_id: str
    feature_role: Literal["primary", "supporting"]
    economic_axis: EconomicAxis
    direction: Direction
    direction_multiplier: Decimal | None
    normalization_method: NormalizationMethod
    full_window: int = Field(gt=1)
    min_history: int = Field(gt=1)
    redundancy_group: str
    normalized_unit: Literal["z_score"] = "z_score"

    @model_validator(mode="after")
    def validate_direction(self) -> "SignalDefinition":
        """Require the exact multiplier implied by the approved direction."""
        expected = {
            Direction.POSITIVE: Decimal(1),
            Direction.INVERSE: Decimal(-1),
            Direction.NEUTRAL_CONTEXT_ONLY: None,
        }[self.direction]
        if self.direction_multiplier != expected:
            raise ValueError(f"{self.signal_id} direction multiplier must be {expected}")
        if self.min_history >= self.full_window:
            raise ValueError(f"{self.signal_id} min_history must be below full_window")
        return self


class SignalCatalog(BaseModel):
    """Validated and hash-bound Stage 3B signal authority."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    normalization_spec_version: str
    normalizer_registry_version: str
    required_feature_spec_version: str
    required_source_registry_version: str
    required_source_registry_hash: str
    distribution_includes_current: bool
    extreme_value_threshold: Decimal = Field(gt=0)
    low_dispersion_relative_threshold: Decimal = Field(gt=0)
    signals: list[SignalDefinition]
    source_path: Path
    specification_hash: str

    def by_signal(self) -> dict[str, SignalDefinition]:
        """Return definitions keyed by stable signal ID."""
        return {item.signal_id: item for item in self.signals}

    def by_feature(self) -> dict[str, SignalDefinition]:
        """Return the one-to-one signal definition for every Stage 3A feature."""
        return {item.feature_id: item for item in self.signals}


class NormalizedSignalResult(BaseModel):
    """Auditable compute-on-demand normalized signal without provider payloads."""

    model_config = ConfigDict(extra="forbid")

    signal_id: str
    feature_id: str
    feature_spec_version: str
    normalization_spec_version: str
    normalization_spec_hash: str
    indicator_id: str
    economic_axis: EconomicAxis
    feature_role: Literal["primary", "supporting"]
    redundancy_group: str
    feature_observation_date: date
    as_of: date | None
    raw_feature_value: Decimal | None
    raw_feature_unit: str
    normalized_value: Decimal | None
    directed_value: Decimal | None
    normalized_unit: str
    normalization_method: NormalizationMethod
    window: int
    min_history: int
    history_start: date | None
    history_end: date | None
    history_count: int
    window_coverage: Decimal
    center: Decimal | None
    scale: Decimal | None
    dispersion_estimator: str
    distribution_includes_current: Literal[False]
    direction: Direction
    direction_multiplier: Decimal | None
    source_policy_version: str
    registry_hash: str
    feature_calculation_hash: str
    input_feature_calculation_hashes: list[str]
    history_feature_calculation_hash: str
    is_point_in_time_safe: bool
    signal_status: SignalStatus
    quality_flags: list[SignalQualityFlag]
    calculation_hash: str
