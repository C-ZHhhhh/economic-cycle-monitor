"""Metadata normalization and semantic validation for configured FRED series."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.data_sources.fred.schemas import FredSeriesMetadata
from app.indicators.schemas import IndicatorDefinition

MetadataStatus = Literal["valid", "valid_with_warning", "invalid"]

FREQUENCY_ALIASES = {
    "d": "daily",
    "daily": "daily",
    "w": "weekly",
    "weekly": "weekly",
    "bw": "biweekly",
    "biweekly": "biweekly",
    "m": "monthly",
    "monthly": "monthly",
    "q": "quarterly",
    "quarterly": "quarterly",
    "a": "annual",
    "annual": "annual",
}

UNIT_TOKENS = {
    "percent": ("percent",),
    "index": ("index",),
    "number": ("number",),
    "hours": ("hours",),
    "ratio": ("ratio",),
    "chained_dollars": ("chained", "dollars"),
    "millions_dollars": ("millions", "dollars"),
    "thousands_persons": ("thousands", "persons"),
    "thousands_units": ("thousands", "units"),
    "dollars_per_hour": ("dollars", "hour"),
}

SEASONAL_TOKENS = {
    "seasonally_adjusted": ("seasonally adjusted",),
    "seasonally_adjusted_annual_rate": ("seasonally adjusted annual rate",),
    "not_seasonally_adjusted": ("not seasonally adjusted",),
}


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


class MetadataValidationResult(BaseModel):
    """Validation outcome that never mutates or substitutes the configured Series ID."""

    model_config = ConfigDict(extra="forbid")

    series_id: str
    status: MetadataStatus
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def validate_series_metadata(
    definition: IndicatorDefinition, metadata: FredSeriesMetadata
) -> MetadataValidationResult:
    """Compare source metadata to configuration and preserve the configured identity."""
    errors: list[str] = []
    warnings: list[str] = []
    if metadata.id != definition.fred_series_id:
        errors.append(
            f"returned Series ID {metadata.id!r} does not equal configured "
            f"{definition.fred_series_id!r}"
        )

    source_frequency = FREQUENCY_ALIASES.get(_normalized(metadata.frequency_short))
    source_frequency = source_frequency or FREQUENCY_ALIASES.get(_normalized(metadata.frequency))
    if source_frequency != definition.observation_frequency:
        errors.append(
            f"frequency {metadata.frequency!r} does not match expected "
            f"{definition.observation_frequency!r}"
        )

    source_units = _normalized(metadata.units)
    unit_tokens = UNIT_TOKENS.get(definition.expected_unit, ())
    if not unit_tokens or not all(token in source_units for token in unit_tokens):
        warnings.append(
            f"units {metadata.units!r} need review against expected {definition.expected_unit!r}"
        )

    source_adjustment = _normalized(metadata.seasonal_adjustment)
    expected_tokens = SEASONAL_TOKENS.get(definition.expected_seasonal_adjustment, ())
    if not expected_tokens or not all(token in source_adjustment for token in expected_tokens):
        errors.append(
            f"seasonal adjustment {metadata.seasonal_adjustment!r} does not match expected "
            f"{definition.expected_seasonal_adjustment!r}"
        )

    title = _normalized(metadata.title)
    missing_keywords = [keyword for keyword in definition.title_keywords if keyword not in title]
    if missing_keywords:
        warnings.append(f"title is missing expected semantic terms: {', '.join(missing_keywords)}")

    status: MetadataStatus = "invalid" if errors else "valid_with_warning" if warnings else "valid"
    return MetadataValidationResult(
        series_id=definition.fred_series_id,
        status=status,
        errors=errors,
        warnings=warnings,
    )
