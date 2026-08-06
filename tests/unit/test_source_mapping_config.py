"""Reviewed BLS source-mapping configuration tests."""

from app.source_mappings.config import load_source_mapping_catalog


def test_bls_catalog_contains_only_the_14_reviewed_series() -> None:
    catalog = load_source_mapping_catalog()

    assert catalog.source == "bls"
    assert len(catalog.mappings) == 14
    assert all(item.mapping_status == "valid" for item in catalog.mappings)
    assert all(item.enabled for item in catalog.mappings)
    assert len({item.source_series_id for item in catalog.mappings}) == 14


def test_jolts_rate_and_level_mappings_are_not_substituted() -> None:
    by_id = {item.indicator_id: item for item in load_source_mapping_catalog().mappings}

    assert by_id["US_JOB_OPENINGS"].source_series_id == "JTS000000000000000JOL"
    assert by_id["US_JOB_OPENINGS"].expected_unit == "thousands_persons"
    assert by_id["US_HIRES"].source_series_id == "JTS000000000000000HIR"
    assert by_id["US_HIRES"].expected_unit == "rate"
    assert by_id["US_QUITS"].source_series_id == "JTS000000000000000QUR"
    assert by_id["US_QUITS"].expected_unit == "rate"


def test_bls_scope_excludes_dol_and_future_official_sources() -> None:
    indicator_ids = {item.indicator_id for item in load_source_mapping_catalog().mappings}

    assert "US_INITIAL_CLAIMS" not in indicator_ids
    assert "US_CONTINUING_CLAIMS" not in indicator_ids
    assert {item.source for item in load_source_mapping_catalog().mappings} == {"bls"}
