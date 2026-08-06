"""Indicator catalog validation tests."""

from decimal import Decimal

from app.indicators.config import load_indicator_catalog


def test_stage_two_catalog_contains_exactly_33_unique_fred_series() -> None:
    catalog = load_indicator_catalog()

    assert catalog.schema_version == "0.2.0"
    assert catalog.stage == "fred_data_foundation"
    assert len(catalog.indicators) == 33
    assert len({item.indicator_id for item in catalog.indicators}) == 33
    assert len({item.fred_series_id for item in catalog.indicators}) == 33


def test_catalog_keeps_stage_three_fields_inert() -> None:
    catalog = load_indicator_catalog()

    assert all(item.direction == "pending_stage3" for item in catalog.indicators)
    assert all(item.weight == Decimal("0") for item in catalog.indicators)
    assert all(item.primary_source == "fred" for item in catalog.indicators)
    assert all(item.fred_series_id == item.primary_source_series_id for item in catalog.indicators)


def test_semantically_sensitive_series_ids_are_preserved() -> None:
    catalog = load_indicator_catalog()
    series_ids = {item.fred_series_id for item in catalog.indicators}

    assert {"RRSFS", "U6RATE", "TEMPHELPS", "AHETPI", "NEWORDER"} <= series_ids
