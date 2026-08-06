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

SEASONAL_TOKENS = {
    "seasonally_adjusted": ("seasonally adjusted",),
    "seasonally_adjusted_annual_rate": ("seasonally adjusted annual rate",),
    "not_seasonally_adjusted": ("not seasonally adjusted",),
}


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _contains_term(normalized_text: str, term: str) -> bool:
    """Match one normalized token or a contiguous multi-token phrase."""
    normalized_term = _normalized(term)
    if " " in normalized_term:
        return normalized_term in normalized_text
    return normalized_term in normalized_text.split()


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
    unit_matches = any(
        alias and all(_contains_term(source_units, token) for token in alias)
        for alias in definition.unit_aliases
    )
    if not unit_matches:
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
    keyword_groups = definition.title_keyword_groups or [
        [keyword] for keyword in definition.title_keywords
    ]
    missing_groups = [
        "/".join(group)
        for group in keyword_groups
        if not any(_contains_term(title, keyword) for keyword in group)
    ]
    if missing_groups:
        warnings.append(f"title is missing expected semantic terms: {', '.join(missing_groups)}")

    status: MetadataStatus = "invalid" if errors else "valid_with_warning" if warnings else "valid"
    return MetadataValidationResult(
        series_id=definition.fred_series_id,
        status=status,
        errors=errors,
        warnings=warnings,
    )
