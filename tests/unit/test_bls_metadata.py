"""Offline semantic checks for optional BLS catalog metadata."""

from app.data_sources.bls.metadata import validate_bls_metadata
from app.data_sources.bls.schemas import BlsSeries
from app.source_mappings.config import load_source_mapping_catalog


def test_seasonal_metadata_mismatch_is_invalid() -> None:
    definition = next(
        item
        for item in load_source_mapping_catalog().mappings
        if item.indicator_id == "US_UNEMPLOYMENT_RATE"
    )
    series = BlsSeries.model_validate(
        {
            "seriesID": definition.source_series_id,
            "catalog": {
                "series_title": "Unemployment Rate",
                "seasonality": "Not seasonally adjusted",
            },
            "data": [],
        }
    )
    result = validate_bls_metadata(definition, series)
    assert result.status == "invalid"
    assert result.errors == ["BLS catalog seasonal adjustment does not match the reviewed mapping"]


def test_absent_catalog_is_warning_not_a_series_id_substitution() -> None:
    definition = load_source_mapping_catalog().mappings[0]
    series = BlsSeries(seriesID=definition.source_series_id, data=[])
    result = validate_bls_metadata(definition, series)
    assert result.status == "valid_with_warning"
    assert result.errors == []
