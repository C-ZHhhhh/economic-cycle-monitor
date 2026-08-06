"""Conservative validation of optional BLS catalog fields."""

import re
from dataclasses import dataclass, field

from app.data_sources.bls.schemas import BlsSeries
from app.source_mappings.schemas import SourceMappingDefinition


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _seasonal(value: str) -> str:
    normalized = value.lower().replace("-", " ").replace("_", " ")
    if "not seasonally" in normalized or "unadjusted" in normalized:
        return "not_seasonally_adjusted"
    if "seasonally" in normalized and "adjust" in normalized:
        return "seasonally_adjusted"
    return normalized.replace(" ", "_")


@dataclass(slots=True)
class BlsMetadataValidation:
    """Errors and warnings from fields actually returned by BLS."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors:
            return "invalid"
        if self.warnings:
            return "valid_with_warning"
        return "valid"


def validate_bls_metadata(
    definition: SourceMappingDefinition, series: BlsSeries
) -> BlsMetadataValidation:
    """Validate exact Series ID and only catalog semantics supplied by BLS."""
    result = BlsMetadataValidation()
    if series.seriesID != definition.source_series_id:
        result.errors.append(
            f"Series ID mismatch: expected {definition.source_series_id}, got {series.seriesID}"
        )
    catalog = series.catalog
    if not catalog:
        result.warnings.append("BLS catalog metadata was not returned for this series")
        return result
    title = str(catalog.get("series_title") or catalog.get("seriesTitle") or "")
    if title:
        expected_tokens = _tokens(definition.official_name) - {
            "in",
            "of",
            "the",
            "and",
            "data",
        }
        actual_tokens = _tokens(title)
        missing = sorted(expected_tokens - actual_tokens)
        if len(missing) > max(2, len(expected_tokens) // 3):
            result.errors.append("BLS catalog title does not match the reviewed series scope")
    seasonality = catalog.get("seasonality") or catalog.get("seasonal")
    if seasonality and _seasonal(str(seasonality)) != definition.expected_seasonal_adjustment:
        result.errors.append("BLS catalog seasonal adjustment does not match the reviewed mapping")
    return result
