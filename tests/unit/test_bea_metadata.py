"""Strict BEA candidate identity validation tests."""

from pathlib import Path

from app.data_sources.bea.metadata import validate_bea_metadata
from app.data_sources.bea.schemas import BeaNipaDataRow
from app.source_mappings.config import load_source_mapping_catalog
from app.source_mappings.schemas import SourceMappingDefinition


def _definition(indicator_id: str = "US_REAL_GDP") -> SourceMappingDefinition:
    catalog = load_source_mapping_catalog(Path("config/data_sources/bea.yaml"))
    return next(item for item in catalog.mappings if item.indicator_id == indicator_id)


def _gdp_row(**changes: str) -> BeaNipaDataRow:
    values = {
        "TableName": "T10106",
        "SeriesCode": "A191RX",
        "LineNumber": "1",
        "LineDescription": "Gross domestic product",
        "TimePeriod": "2025Q1",
        "METRIC_NAME": "Chained Dollars",
        "CL_UNIT": "Billions of Chained 2017 Dollars",
        "UNIT_MULT": "9",
        "DataValue": "23,548.210",
    }
    values.update(changes)
    return BeaNipaDataRow.model_validate(values)


def test_exact_identity_stays_pending_until_cross_source_review() -> None:
    result = validate_bea_metadata(_definition(), [_gdp_row()])
    assert result.identity_confirmed is True
    assert result.status == "pending_review"
    assert result.errors == []
    assert "remains pending" in result.warnings[0]


def test_correct_series_with_wrong_line_stops_candidate() -> None:
    result = validate_bea_metadata(_definition(), [_gdp_row(LineNumber="2")])
    assert result.identity_confirmed is False
    assert result.status == "pending_review"
    assert "LineNumber mismatch" in result.errors[0]
    assert result.returned_line_numbers == ["2"]


def test_correct_line_with_wrong_series_stops_candidate() -> None:
    result = validate_bea_metadata(_definition(), [_gdp_row(SeriesCode="WRONG")])
    assert result.identity_confirmed is False
    assert "SeriesCode mismatch" in result.errors[0]
    assert "WRONG" in result.errors[0]


def test_non_candidate_multiplier_is_warning_when_normalizable() -> None:
    result = validate_bea_metadata(
        _definition(),
        [_gdp_row(UNIT_MULT="6", DataValue="23,548,210")],
    )
    assert result.identity_confirmed is True
    assert any("mathematically normalizable" in item for item in result.warnings)


def test_frequency_and_economic_meaning_are_required() -> None:
    result = validate_bea_metadata(
        _definition(),
        [_gdp_row(TimePeriod="2025M01", LineDescription="Unrelated amount")],
    )
    assert result.identity_confirmed is False
    assert any("economic concept" in item for item in result.errors)
    assert any("frequency Q" in item for item in result.errors)


def test_t20804_headline_and_core_validate_independently() -> None:
    headline = BeaNipaDataRow.model_validate(
        {
            "TableName": "T20804",
            "SeriesCode": "DPCERG",
            "LineNumber": "1",
            "LineDescription": "Personal consumption expenditures (PCE)",
            "TimePeriod": "2025M01",
            "METRIC_NAME": "Fisher Price Index",
            "CL_UNIT": "Index 2017=100",
            "UNIT_MULT": "0",
            "DataValue": "125.100",
        }
    )
    core = headline.model_copy(
        update={
            "SeriesCode": "DPCCRG",
            "LineNumber": "6",
            "LineDescription": "PCE excluding food and energy",
            "DataValue": "124.900",
        }
    )
    rows = [headline, core]
    assert validate_bea_metadata(_definition("US_PCE_PRICE"), rows).identity_confirmed
    assert validate_bea_metadata(_definition("US_CORE_PCE_PRICE"), rows).identity_confirmed
