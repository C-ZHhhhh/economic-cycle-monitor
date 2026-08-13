"""Reviewed BLS source-mapping configuration tests."""

from app.source_mappings.config import (
    load_source_mapping_catalog,
    load_source_mapping_catalog_for,
)


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


def test_bea_catalog_contains_only_five_live_reviewed_mappings() -> None:
    catalog = load_source_mapping_catalog_for("bea")

    assert catalog.source == "bea"
    assert len(catalog.mappings) == 5
    statuses = {item.indicator_id: item.mapping_status for item in catalog.mappings}
    assert statuses == {
        "US_REAL_GDP": "valid_with_warning",
        "US_REAL_PCE": "valid_with_warning",
        "US_REAL_DISPOSABLE_INCOME": "valid_with_warning",
        "US_PCE_PRICE": "valid",
        "US_CORE_PCE_PRICE": "valid",
    }
    assert all(item.enabled for item in catalog.mappings)
    assert {item.source_parameters["table_name"] for item in catalog.mappings} == {
        "T10106",
        "T20806",
        "T20600",
        "T20804",
    }
    assert sum(item.source_parameters["table_name"] == "T20804" for item in catalog.mappings) == 2
    assert all(item.mapping_version == "0.4.1" for item in catalog.mappings)
    assert (
        next(
            item for item in catalog.mappings if item.indicator_id == "US_CORE_PCE_PRICE"
        ).source_parameters["line_number"]
        == "25"
    )


def test_fed_and_dol_candidates_remain_pending_until_independent_live_gates() -> None:
    fed = load_source_mapping_catalog_for("federal_reserve")
    dol = load_source_mapping_catalog_for("dol")

    assert len(fed.mappings) == 4
    assert len(dol.mappings) == 2
    statuses = {item.indicator_id: item.mapping_status for item in [*fed.mappings, *dol.mappings]}
    assert statuses["US_INDUSTRIAL_PRODUCTION"] == "valid"
    assert statuses["US_MANUFACTURING_CAPACITY"] == "valid"
    assert statuses["US_CONSUMER_CREDIT"] == "valid"
    assert statuses["US_REVOLVING_CREDIT"] == "valid"
    assert statuses["US_INITIAL_CLAIMS"] == "valid_with_warning"
    assert statuses["US_CONTINUING_CLAIMS"] == "valid_with_warning"
    assert {item.source_series_id for item in fed.mappings} == {
        "IP.B50001.S",
        "CAPUTL.B00004.S",
        "DTCTL.M",
        "DTCTLR.M",
    }
    capacity = next(
        item for item in fed.mappings if item.indicator_id == "US_MANUFACTURING_CAPACITY"
    )
    assert capacity.fred_series_id == "CUMFNS"
    assert capacity.source_parameters["transport_series_code"] == "B00004"
    assert capacity.source_parameters["description_keywords"] == ["manufacturing", "sic"]
    assert capacity.mapping_status == "valid"
    g19 = [item for item in fed.mappings if item.source_dataset == "G19"]
    assert len(g19) == 2
    assert {item.source_parameters["data_path"] for item in g19} == {
        "releases/g19/data/FRB_g19_xml.zip"
    }
    assert {item.source_unit for item in g19} == {"millions_usd"}
    assert {item.normalization_rule for item in g19} == {"identity"}
    assert {item.source_parameters["wire_frequency"] for item in g19} == {"129"}
    assert {item.source_parameters["data_representation"] for item in g19} == {"MILLDOLL"}
    assert {item.source_parameters["unit_multiplier"] for item in g19} == {"1000000"}
    assert all(item.fallback_source == "fred" for item in [*fed.mappings, *dol.mappings])


def test_production_fed_mapping_has_no_ddp_package_dependency() -> None:
    catalog = load_source_mapping_catalog_for("federal_reserve")

    for mapping in catalog.mappings:
        serialized = str(mapping.source_parameters).lower()
        assert "package" not in serialized
        assert "datadownload" not in serialized
        assert mapping.source_parameters["transport"] in {"official_text", "official_sdmx"}


def test_dol_claims_use_live_discovered_paths_with_independent_value_paths() -> None:
    catalog = load_source_mapping_catalog_for("dol")
    by_id = {item.indicator_id: item for item in catalog.mappings}
    initial = by_id["US_INITIAL_CLAIMS"].source_parameters
    continued = by_id["US_CONTINUING_CLAIMS"].source_parameters

    assert initial["root_tag"] == "r539cyNational"
    assert initial["row_path"] == "./week"
    assert initial["observation_path"] == "weekEnded"
    assert initial["value_path"] == "InitialClaims/SA"
    assert initial["expected_wire_values"] == {"root_tag": ["r539cyNational"]}
    assert continued["root_tag"] == "r539cyNational"
    assert continued["row_path"] == "./week"
    assert continued["observation_path"] == "weekEnded"
    assert continued["value_path"] == "ContinuedClaims/SA"
    assert continued["expected_wire_values"] == {"root_tag": ["r539cyNational"]}
