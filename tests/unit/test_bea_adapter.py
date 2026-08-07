"""BEA period, value, unit multiplier, and note normalization tests."""

from datetime import date
from decimal import Decimal

import pytest

from app.data_sources.bea.adapter import BeaAdapter
from app.data_sources.bea.exceptions import BeaResponseError
from app.data_sources.bea.schemas import BeaNipaDataRow, BeaNote


def _row(**changes: str) -> BeaNipaDataRow:
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
        "NoteRef": "1, T10106",
    }
    values.update(changes)
    return BeaNipaDataRow.model_validate(values)


def test_quarterly_and_monthly_periods_remain_distinct() -> None:
    assert BeaAdapter.parse_period("2025Q4", frequency="Q") == date(2025, 10, 1)
    assert BeaAdapter.parse_period("2025M02", frequency="M") == date(2025, 2, 1)
    with pytest.raises(BeaResponseError, match="does not match"):
        BeaAdapter.parse_period("2025Q1", frequency="M")


def test_unit_multiplier_is_applied_to_target_scale() -> None:
    assert BeaAdapter.parse_value(
        "23,548.210", unit_multiplier="9", target_unit_multiplier=9
    ) == Decimal("23548.210")
    assert BeaAdapter.parse_value(
        "23,548,210", unit_multiplier="6", target_unit_multiplier=9
    ) == Decimal("23548.210")


@pytest.mark.parametrize("missing", ["", "--", "...", ".", "(NA)"])
def test_missing_values_are_not_zero(missing: str) -> None:
    assert BeaAdapter.parse_value(missing, unit_multiplier="9", target_unit_multiplier=9) is None


def test_raw_and_canonical_fields_and_notes_are_retained() -> None:
    observations = BeaAdapter.observations(
        [_row()],
        [BeaNote(NoteRef="1", NoteText="Estimate note")],
        frequency="Q",
        target_unit_multiplier=9,
    )
    item = observations[0]
    assert item.value == Decimal("23548.210")
    assert item.details["raw_value"] == "23,548.210"
    assert item.details["unit_mult"] == "9"
    assert item.details["canonical_value"] == "23548.210"
    assert item.footnotes[0] == {"code": "1", "text": "Estimate note"}
