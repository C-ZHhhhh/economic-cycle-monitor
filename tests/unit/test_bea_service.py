"""Small offline tests for BEA request window planning."""

import pytest

from app.data_pipeline.bea_service import bea_year_is_available, split_bea_years
from app.data_sources.bea.schemas import BeaParameterValue


def test_split_bea_years_uses_explicit_bounded_batches() -> None:
    assert split_bea_years(2000, 2011, 5) == [
        [2000, 2001, 2002, 2003, 2004],
        [2005, 2006, 2007, 2008, 2009],
        [2010, 2011],
    ]


def test_split_bea_years_rejects_reverse_range() -> None:
    with pytest.raises(ValueError, match="must not be after"):
        split_bea_years(2025, 2024, 5)


def test_bea_year_availability_uses_table_frequency_bounds() -> None:
    values = [
        BeaParameterValue.model_validate(
            {
                "TableName": "T10106",
                "FirstAnnualYear": "1929",
                "LastAnnualYear": "2025",
                "FirstQuarterlyYear": "1947",
                "LastQuarterlyYear": "2026",
                "FirstMonthlyYear": "0",
                "LastMonthlyYear": "0",
            }
        )
    ]

    assert bea_year_is_available(values, table_name="T10106", frequency="Q", year=2026)
    assert not bea_year_is_available(values, table_name="T10106", frequency="M", year=2026)
    assert not bea_year_is_available(values, table_name="T20804", frequency="M", year=2026)
