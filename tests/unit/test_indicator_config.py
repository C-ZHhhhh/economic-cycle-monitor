"""Indicator catalog validation tests."""

from decimal import Decimal

from app.indicators.config import load_indicator_catalog
from app.source_mappings.config import load_source_mapping_catalog


def test_stage_two_catalog_contains_exactly_33_unique_fred_series() -> None:
    catalog = load_indicator_catalog()

    assert catalog.schema_version == "0.3.0"
    assert catalog.stage == "official_source_foundation"
    assert len(catalog.indicators) == 33
    assert len({item.indicator_id for item in catalog.indicators}) == 33
    assert len({item.fred_series_id for item in catalog.indicators}) == 33


def test_catalog_keeps_stage_three_fields_inert() -> None:
    catalog = load_indicator_catalog()

    assert all(item.direction == "pending_stage3" for item in catalog.indicators)
    assert all(item.weight == Decimal("0") for item in catalog.indicators)
    bls = {item.indicator_id: item for item in load_source_mapping_catalog().mappings}
    assert len(bls) == 14
    for item in catalog.indicators:
        if item.indicator_id in bls:
            assert item.primary_source == "bls"
            assert item.primary_source_series_id == bls[item.indicator_id].source_series_id
            assert item.fallback_source == "fred"
        else:
            assert item.primary_source == "fred"
            assert item.fred_series_id == item.primary_source_series_id


def test_semantically_sensitive_series_ids_are_preserved() -> None:
    catalog = load_indicator_catalog()
    series_ids = {item.fred_series_id for item in catalog.indicators}

    assert {"RRSFS", "U6RATE", "TEMPHELPS", "AHETPI", "NEWORDER"} <= series_ids


def test_metadata_decisions_preserve_stable_ids_and_use_configured_aliases() -> None:
    catalog = load_indicator_catalog()
    by_id = {item.indicator_id: item for item in catalog.indicators}

    assert by_id["US_HIRES"].fred_series_id == "JTSHIR"
    assert by_id["US_HIRES"].name == "Hires Rate"
    assert by_id["US_HIRES"].expected_unit == "rate"
    assert by_id["US_QUITS"].fred_series_id == "JTSQUR"
    assert by_id["US_QUITS"].name == "Quits Rate"
    assert by_id["US_QUITS"].expected_unit == "rate"
    assert ["level", "thousands"] in by_id["US_JOB_OPENINGS"].unit_aliases
    assert by_id["US_REAL_RETAIL_SALES"].expected_unit == ("millions_1982_84_cpi_adjusted_dollars")
    assert ["permit", "permits", "building permits"] in by_id[
        "US_BUILDING_PERMITS"
    ].title_keyword_groups
