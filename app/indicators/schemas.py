"""Typed indicator configuration schemas."""

from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IndicatorDefinition(BaseModel):
    """One versioned US macro indicator definition."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    name: str
    description: str
    module: str
    owner_agent: str
    fred_series_id: str
    primary_source: str
    primary_source_series_id: str
    fallback_source: str | None = None
    observation_frequency: str
    release_frequency: str
    expected_unit: str
    expected_seasonal_adjustment: str
    direction: str
    weight: Decimal
    max_staleness_days: int = Field(gt=0)
    revision_policy: str
    enabled: bool
    config_version: str
    title_keywords: list[str] = Field(default_factory=list)


class IndicatorCatalog(BaseModel):
    """Validated Stage 2 indicator catalog."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    agent: str
    country: str
    stage: str
    indicators: list[IndicatorDefinition]
    source_path: Path

    @model_validator(mode="after")
    def require_unique_ids(self) -> "IndicatorCatalog":
        indicator_ids = [item.indicator_id for item in self.indicators]
        series_ids = [item.fred_series_id for item in self.indicators]
        if len(indicator_ids) != len(set(indicator_ids)):
            raise ValueError("indicator_id values must be unique")
        if len(series_ids) != len(set(series_ids)):
            raise ValueError("fred_series_id values must be unique")
        return self


class SeedResult(BaseModel):
    """Summary of an idempotent catalog-to-database synchronization."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    disabled: int = 0
