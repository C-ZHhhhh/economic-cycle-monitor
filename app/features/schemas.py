"""Typed Stage 3A feature specification and result contracts."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.source_registry.revision_schemas import RevisionLayerEffect


class FormulaId(StrEnum):
    """Closed formula vocabulary; configuration cannot execute expressions."""

    LEVEL = "LEVEL"
    DIFF_K = "DIFF_K"
    POINT_DIFF_K = "POINT_DIFF_K"
    AVG_PERIOD_DIFF_K = "AVG_PERIOD_DIFF_K"
    PCT_CHANGE_K = "PCT_CHANGE_K"
    MONTHLY_ANNUALIZED_K = "MONTHLY_ANNUALIZED_K"
    MONTHLY_YOY = "MONTHLY_YOY"
    QUARTERLY_QOQ_ANNUALIZED = "QUARTERLY_QOQ_ANNUALIZED"
    QUARTERLY_YOY = "QUARTERLY_YOY"
    MOVING_AVERAGE_K = "MOVING_AVERAGE_K"
    MA3_BLOCK_GROWTH_ANNUALIZED = "MA3_BLOCK_GROWTH_ANNUALIZED"
    GROWTH_ACCEL_QOQ_1Q = "GROWTH_ACCEL_QOQ_1Q"
    GROWTH_ACCEL_3M_NONOVERLAP = "GROWTH_ACCEL_3M_NONOVERLAP"
    MA4W_CHANGE_KW = "MA4W_CHANGE_KW"
    MA4W_YOY_PCT = "MA4W_YOY_PCT"


class FeatureStatus(StrEnum):
    """Fail-closed computation states for one feature date."""

    OK = "OK"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    MISSING_INPUT_PERIOD = "MISSING_INPUT_PERIOD"
    INVALID_DENOMINATOR = "INVALID_DENOMINATOR"
    NONFINITE_INPUT = "NONFINITE_INPUT"
    NONFINITE_RESULT = "NONFINITE_RESULT"
    PIT_UNSAFE = "PIT_UNSAFE"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    REGISTRY_HASH_MISMATCH = "REGISTRY_HASH_MISMATCH"
    UNSUPPORTED_TRANSFORM = "UNSUPPORTED_TRANSFORM"
    UNSUPPORTED_UNIT = "UNSUPPORTED_UNIT"


class QualityFlag(StrEnum):
    """Non-destructive context retained alongside a feature result."""

    FALLBACK_INPUT = "FALLBACK_INPUT"
    STALE_INPUT = "STALE_INPUT"
    DEGRADED_INPUT = "DEGRADED_INPUT"
    RELEASE_DATE_UNAVAILABLE = "RELEASE_DATE_UNAVAILABLE"
    REVISION_CHANGED_RESULT = "REVISION_CHANGED_RESULT"
    MIXED_INPUT_SOURCES = "MIXED_INPUT_SOURCES"


class FeatureDefinition(BaseModel):
    """One stable feature selected for an indicator."""

    model_config = ConfigDict(extra="forbid")

    feature_id: str
    display_name: str
    description: str
    role: Literal["primary", "supporting"]
    formula_id: FormulaId
    window: int = Field(ge=1)
    output_unit: str


class IndicatorFeatureSpec(BaseModel):
    """Approved Stage 3A feature set for one canonical indicator."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    economic_category: str
    archetype: str
    features: list[FeatureDefinition]

    @model_validator(mode="after")
    def validate_primary(self) -> "IndicatorFeatureSpec":
        """Require exactly one primary feature per indicator."""
        if sum(item.role == "primary" for item in self.features) != 1:
            raise ValueError(f"{self.indicator_id} must define exactly one primary feature")
        return self


class FeatureCatalog(BaseModel):
    """Validated and hash-bound feature specification authority."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    feature_spec_version: str
    formula_registry_version: str
    required_source_registry_version: str
    required_source_registry_hash: str
    indicators: list[IndicatorFeatureSpec]
    source_path: Path
    specification_hash: str

    def by_indicator(self) -> dict[str, IndicatorFeatureSpec]:
        """Return indicator specifications keyed by stable identifier."""
        return {item.indicator_id: item for item in self.indicators}

    def by_feature(self) -> dict[str, tuple[IndicatorFeatureSpec, FeatureDefinition]]:
        """Return feature definitions with their owning indicator specification."""
        return {
            feature.feature_id: (indicator, feature)
            for indicator in self.indicators
            for feature in indicator.features
        }


class FeatureResult(BaseModel):
    """Auditable compute-on-demand feature result without raw provider payloads."""

    model_config = ConfigDict(extra="forbid")

    feature_id: str
    display_name: str
    description: str
    feature_spec_version: str
    formula_registry_version: str
    indicator_id: str
    feature_observation_date: date
    as_of: date | None
    value: Decimal | None
    unit: str
    formula_id: FormulaId
    window: int
    input_count: int
    expected_input_count: int
    input_observation_dates: list[date]
    input_sources: list[str]
    input_source_identities: list[str]
    input_mapping_versions: list[str]
    input_release_dates: list[date | None]
    input_retrieved_at: list[datetime | None]
    input_vintage_dates: list[date | None]
    source_policy_version: str
    registry_hash: str
    is_point_in_time_safe: bool
    feature_status: FeatureStatus
    quality_flags: list[QualityFlag]
    calculation_hash: str
    revision_provenance_version: str | None = None
    revision_provenance_hash: str | None = None
    revision_evidence_hash: str | None = None
    revision_effect: RevisionLayerEffect | None = None
