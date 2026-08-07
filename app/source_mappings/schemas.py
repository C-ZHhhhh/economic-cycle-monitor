"""Typed schemas for reusable official-source mappings."""

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MappingStatus = Literal["valid", "valid_with_warning", "pending_review", "invalid", "unavailable"]


class SourceMappingDefinition(BaseModel):
    """One reviewed mapping between an internal indicator and an official series."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    fred_series_id: str
    source: str
    source_series_id: str
    source_dataset: str
    source_parameters: dict[str, Any] = Field(default_factory=dict)
    official_name: str
    source_unit: str
    expected_unit: str
    expected_frequency: str
    expected_seasonal_adjustment: str
    source_priority: int = Field(ge=1)
    fallback_source: str
    vintage_source: str
    normalization_rule: str
    comparison_tolerance: Decimal = Field(ge=0)
    distribution_lag_days: int = Field(ge=0)
    revision_lookback_years: int = Field(ge=1)
    mapping_status: MappingStatus
    enabled: bool
    mapping_version: str
    validation_url: str
    notes: str


class SourceMappingCatalog(BaseModel):
    """Versioned collection of mappings for one official source."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    source: str
    mappings: list[SourceMappingDefinition]
    source_path: Path

    @model_validator(mode="after")
    def require_consistent_unique_mappings(self) -> "SourceMappingCatalog":
        keys = [(item.indicator_id, item.source_series_id) for item in self.mappings]
        if len(keys) != len(set(keys)):
            raise ValueError("Source mapping indicator/series pairs must be unique")
        if any(item.source != self.source for item in self.mappings):
            raise ValueError("Every mapping source must match the catalog source")
        return self


class SourceMappingSeedResult(BaseModel):
    """Counts from an idempotent mapping synchronization."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    disabled: int = 0
