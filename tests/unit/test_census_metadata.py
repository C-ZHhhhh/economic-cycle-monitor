"""Pending-only Census mapping validation tests."""

from app.data_sources.census.metadata import validate_census_metadata
from app.data_sources.census.schemas import CensusRow
from app.source_mappings.config import load_source_mapping_catalog_for
from app.source_mappings.schemas import SourceMappingDefinition


def _definition() -> SourceMappingDefinition:
    return next(
        item
        for item in load_source_mapping_catalog_for("census").mappings
        if item.indicator_id == "US_HOUSING_STARTS"
    )


def _row(**updates: str) -> CensusRow:
    values = {
        "cell_value": "1427",
        "category_code": "ASTARTS",
        "data_type_code": "TOTAL",
        "seasonally_adj": "yes",
        "geo_level_code": "US",
        "time": "2026",
        "time_slot_id": "06",
        "time_slot_date": "2026-06",
    }
    values.update(updates)
    return CensusRow.model_validate(values)


def _variables() -> dict[str, object]:
    return {
        "variables": {
            key: {}
            for key in (
                "cell_value",
                "category_code",
                "data_type_code",
                "seasonally_adj",
                "geo_level_code",
                "time",
            )
        }
    }


def test_exact_candidate_stays_pending_and_reports_wire_values() -> None:
    result = validate_census_metadata(
        _definition(), {"dataset": "resconst"}, _variables(), [_row()]
    )

    assert result.status == "pending_review"
    assert result.identity_confirmed
    assert result.returned_seasonal_values == ["yes"]
    assert result.returned_geography_values == ["US"]
    assert any("pending explicit live review" in item for item in result.warnings)


def test_category_match_with_wrong_data_type_is_invalid() -> None:
    result = validate_census_metadata(
        _definition(),
        {"dataset": "resconst"},
        _variables(),
        [_row(data_type_code="SINGLE")],
    )

    assert result.status == "invalid"
    assert not result.identity_confirmed
    assert any("unexpected_data_type" in item for item in result.errors)
