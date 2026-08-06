"""FRED metadata validation tests."""

import pytest

from app.data_sources.fred.metadata import validate_series_metadata
from app.data_sources.fred.schemas import FredSeriesMetadata
from app.indicators.config import load_indicator_catalog


def _gdpc1_metadata(**overrides: object) -> FredSeriesMetadata:
    values: dict[str, object] = {
        "id": "GDPC1",
        "realtime_start": "2026-01-01",
        "realtime_end": "2026-01-01",
        "title": "Real Gross Domestic Product",
        "observation_start": "1947-01-01",
        "observation_end": "2025-10-01",
        "frequency": "Quarterly",
        "frequency_short": "Q",
        "units": "Billions of Chained 2017 Dollars",
        "units_short": "Bil. of Chn. 2017 $",
        "seasonal_adjustment": "Seasonally Adjusted Annual Rate",
        "seasonal_adjustment_short": "SAAR",
        "last_updated": "2026-01-22 07:48:01-06",
        "popularity": 92,
        "notes": "Test fixture only.",
    }
    values.update(overrides)
    return FredSeriesMetadata.model_validate(values)


def test_matching_metadata_is_valid() -> None:
    definition = load_indicator_catalog().indicators[0]

    result = validate_series_metadata(definition, _gdpc1_metadata())

    assert result.status == "valid"
    assert result.series_id == "GDPC1"


def test_semantic_warning_never_substitutes_series_id() -> None:
    definition = load_indicator_catalog().indicators[0]

    result = validate_series_metadata(
        definition, _gdpc1_metadata(title="Gross Product Placeholder")
    )

    assert result.status == "valid_with_warning"
    assert result.series_id == definition.fred_series_id == "GDPC1"
    assert result.warnings


def test_identity_and_frequency_mismatches_are_invalid() -> None:
    definition = load_indicator_catalog().indicators[0]

    result = validate_series_metadata(
        definition, _gdpc1_metadata(id="OTHER", frequency="Monthly", frequency_short="M")
    )

    assert result.status == "invalid"
    assert len(result.errors) == 2
    assert result.series_id == "GDPC1"


@pytest.mark.parametrize(
    ("indicator_id", "title", "units", "seasonal_adjustment"),
    [
        ("US_HIRES", "Hires: Total Nonfarm", "Rate", "Seasonally Adjusted"),
        ("US_QUITS", "Quits: Total Nonfarm", "Rate", "Seasonally Adjusted"),
        (
            "US_JOB_OPENINGS",
            "Job Openings: Total Nonfarm",
            "Level in Thousands",
            "Seasonally Adjusted",
        ),
        (
            "US_REAL_RETAIL_SALES",
            "Advance Real Retail and Food Services Sales",
            "Millions of 1982-84 CPI Adjusted Dollars",
            "Seasonally Adjusted",
        ),
        (
            "US_BUILDING_PERMITS",
            "New Privately-Owned Housing Units Authorized in Permit-Issuing Places: Total Units",
            "Thousands of Units",
            "Seasonally Adjusted Annual Rate",
        ),
    ],
)
def test_decided_metadata_semantics_are_valid(
    indicator_id: str,
    title: str,
    units: str,
    seasonal_adjustment: str,
) -> None:
    definitions = {item.indicator_id: item for item in load_indicator_catalog().indicators}
    definition = definitions[indicator_id]
    metadata = _gdpc1_metadata(
        id=definition.fred_series_id,
        title=title,
        frequency="Monthly",
        frequency_short="M",
        units=units,
        seasonal_adjustment=seasonal_adjustment,
    )

    result = validate_series_metadata(definition, metadata)

    assert result.status == "valid"
    assert result.errors == []
    assert result.warnings == []


def test_title_keyword_groups_do_not_disable_title_validation() -> None:
    definition = next(
        item
        for item in load_indicator_catalog().indicators
        if item.indicator_id == "US_BUILDING_PERMITS"
    )
    metadata = _gdpc1_metadata(
        id="PERMIT",
        title="Unrelated Placeholder Series",
        frequency="Monthly",
        frequency_short="M",
        units="Thousands of Units",
        seasonal_adjustment="Seasonally Adjusted Annual Rate",
    )

    result = validate_series_metadata(definition, metadata)

    assert result.status == "valid_with_warning"
    assert any("title is missing" in warning for warning in result.warnings)
