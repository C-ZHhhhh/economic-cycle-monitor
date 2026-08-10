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
        "UNIT_MULT": "6",
        "DataValue": "23,548,210",
    }
    values.update(changes)
    return BeaNipaDataRow.model_validate(values)


def test_exact_reviewed_identity_retains_reviewed_status() -> None:
    result = validate_bea_metadata(_definition(), [_gdp_row()])
    assert result.identity_confirmed is True
    assert result.status == "valid_with_warning"
    assert result.errors == []
    assert not any("remains pending" in item for item in result.warnings)


def test_pending_identity_is_not_automatically_promoted() -> None:
    definition = _definition().model_copy(update={"mapping_status": "pending_review"})

    result = validate_bea_metadata(definition, [_gdp_row()])

    assert result.identity_confirmed is True
    assert result.status == "pending_review"
    assert any("remains pending" in item for item in result.warnings)


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
        [_gdp_row(UNIT_MULT="9", DataValue="23,548.210")],
    )
    assert result.identity_confirmed is True
    assert any("mathematically normalizable" in item for item in result.warnings)


def test_live_shape_chained_dollars_level_is_normalizable_with_reference_warning() -> None:
    result = validate_bea_metadata(
        _definition(),
        [
            _gdp_row(
                METRIC_NAME="Chained Dollars",
                CL_UNIT="Level",
                UNIT_MULT="6",
                DataValue="24,180,419",
            )
        ],
    )

    assert result.identity_confirmed is True
    assert result.errors == []
    assert any("reference year" in item for item in result.warnings)
    assert not any("mathematically normalizable" in item for item in result.warnings)


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
            "CL_UNIT": "Level",
            "UNIT_MULT": "0",
            "DataValue": "125.100",
        }
    )
    core = headline.model_copy(
        update={
            "SeriesCode": "DPCCRG",
            "LineNumber": "25",
            "LineDescription": "PCE excluding food and energy",
            "DataValue": "124.900",
        }
    )
    rows = [headline, core]
    assert validate_bea_metadata(_definition("US_PCE_PRICE"), rows).identity_confirmed
    assert validate_bea_metadata(_definition("US_CORE_PCE_PRICE"), rows).identity_confirmed


def test_core_pce_old_candidate_line_six_is_rejected() -> None:
    row = BeaNipaDataRow.model_validate(
        {
            "TableName": "T20804",
            "SeriesCode": "DPCCRG",
            "LineNumber": "6",
            "LineDescription": "PCE excluding food and energy",
            "TimePeriod": "2025M01",
            "METRIC_NAME": "Fisher Price Index",
            "CL_UNIT": "Level",
            "UNIT_MULT": "0",
            "DataValue": "124.900",
        }
    )

    result = validate_bea_metadata(_definition("US_CORE_PCE_PRICE"), [row])

    assert result.identity_confirmed is False
    assert result.errors == ["LineNumber mismatch: expected 25, returned ['6'] for DPCCRG"]


def test_fisher_price_index_level_zero_without_reference_year_is_valid() -> None:
    row = BeaNipaDataRow.model_validate(
        {
            "TableName": "T20804",
            "SeriesCode": "DPCCRG",
            "LineNumber": "25",
            "LineDescription": "PCE excluding food and energy",
            "TimePeriod": "2025M01",
            "METRIC_NAME": "Fisher Price Index",
            "CL_UNIT": "Level",
            "UNIT_MULT": "0",
            "DataValue": "124.900",
        }
    )

    result = validate_bea_metadata(_definition("US_CORE_PCE_PRICE"), [row])

    assert result.identity_confirmed is True
    assert result.errors == []
    assert not any("reference year" in item for item in result.warnings)


def test_non_price_index_metric_and_wrong_multiplier_are_rejected() -> None:
    row = BeaNipaDataRow.model_validate(
        {
            "TableName": "T20804",
            "SeriesCode": "DPCCRG",
            "LineNumber": "25",
            "LineDescription": "PCE excluding food and energy",
            "TimePeriod": "2025M01",
            "METRIC_NAME": "Chained Dollars",
            "CL_UNIT": "Level",
            "UNIT_MULT": "6",
            "DataValue": "124,900,000",
        }
    )

    result = validate_bea_metadata(_definition("US_CORE_PCE_PRICE"), [row])

    assert result.identity_confirmed is False
    assert any("Metric_Name" in item for item in result.errors)
    assert any("price-index UNIT_MULT mismatch" in item for item in result.errors)
