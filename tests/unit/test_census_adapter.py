"""Precision, time, and exact-identity tests for Census EITS rows."""

from datetime import date
from decimal import Decimal

import pytest

from app.data_sources.census.adapter import CensusAdapter
from app.data_sources.census.exceptions import CensusIdentityError
from app.data_sources.census.identity import (
    census_source_identity,
    census_source_identity_hash,
)
from app.data_sources.census.schemas import CensusMappingIdentity, CensusRow


def _identity(**updates: object) -> CensusMappingIdentity:
    values: dict[str, object] = {
        "mapping_type": "DIRECT",
        "program": "New Residential Construction",
        "dataset": "resconst",
        "category_code": "ASTARTS",
        "data_type_code": "TOTAL",
        "seasonally_adjusted": True,
        "seasonally_adj": "yes",
        "geography": "United States national",
        "geo_level_code": "US",
        "geography_query": "us:*",
        "identity_fields": [],
        "first_available_period": "1959-01",
        "source_unit_code": "K",
        "scale": 1000,
    }
    values.update(updates)
    return CensusMappingIdentity.model_validate(values)


def _row(**updates: object) -> CensusRow:
    values: dict[str, object] = {
        "cell_value": "1427.00000000000000000001",
        "category_code": "ASTARTS",
        "data_type_code": "TOTAL",
        "seasonally_adj": "yes",
        "geo_level_code": "US",
        "time": "2026",
        "time_slot_id": "06",
        "time_slot_date": "2026-06",
        "program_code": "RESCONST",
    }
    values.update(updates)
    return CensusRow.model_validate(values)


def test_decimal_precision_and_raw_cell_value_are_preserved() -> None:
    observations = CensusAdapter.observations([_row()], _identity(), today=date(2026, 8, 11))

    assert observations[0].value == Decimal("1427.00000000000000000001")
    assert observations[0].details["raw_cell_value"] == "1427.00000000000000000001"
    assert observations[0].details["canonical_value"] == "1427.00000000000000000001"


def test_future_and_missing_slots_are_not_observations() -> None:
    rows = [
        _row(time_slot_date="2026-06", cell_value="1427"),
        _row(time_slot_date="2026-09", time_slot_id="09", cell_value="1500"),
        _row(time_slot_date="2026-07", time_slot_id="07", cell_value="NA"),
    ]

    observations = CensusAdapter.observations(rows, _identity(), today=date(2026, 8, 11))

    assert [(item.date, item.value) for item in observations] == [
        (date(2026, 6, 1), Decimal("1427"))
    ]


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"data_type_code": "SINGLE"}, "unexpected_data_type"),
        ({"seasonally_adj": "no"}, "unexpected_seasonal_adjustment"),
        ({"geo_level_code": "WE"}, "unexpected_geography"),
    ],
)
def test_exact_identity_dimensions_are_required(updates: dict[str, object], message: str) -> None:
    with pytest.raises(CensusIdentityError, match=message):
        CensusAdapter.observations([_row(**updates)], _identity())


def test_pending_wire_identity_cannot_generate_observations() -> None:
    with pytest.raises(CensusIdentityError, match="pending live confirmation"):
        CensusAdapter.observations([_row()], _identity(seasonally_adj=None, geo_level_code=None))


def test_identity_hash_changes_for_every_official_dimension() -> None:
    base = census_source_identity(_identity(), _row())
    base_hash = census_source_identity_hash(base)

    for field, value in {
        "dataset": "other",
        "program": "other",
        "category_code": "OTHER",
        "data_type_code": "OTHER",
        "seasonally_adj": "no",
        "geo_level_code": "WE",
        "geography": "West",
    }.items():
        changed = {**base, field: value}
        assert census_source_identity_hash(changed) != base_hash
