"""Offline Census candidate mapping policy tests."""

from app.data_sources.census.schemas import CensusMappingIdentity
from app.source_mappings.config import load_source_mapping_catalog_for


def test_census_catalog_has_six_reviewed_direct_mappings() -> None:
    catalog = load_source_mapping_catalog_for("census")
    identities = {
        item.indicator_id: CensusMappingIdentity.model_validate(item.source_parameters)
        for item in catalog.mappings
    }
    direct = {key for key, value in identities.items() if value.mapping_type == "DIRECT"}

    assert catalog.source == "census"
    assert len(catalog.mappings) == 7
    assert len(direct) == 6
    assert all(item.mapping_status == "valid" for item in catalog.mappings)
    assert identities["US_REAL_RETAIL_SALES"].mapping_type == "SUPPORTING_COMPONENT"
    assert identities["US_REAL_RETAIL_SALES"].dataset == "marts"
    assert identities["US_REAL_RETAIL_SALES"].category_code == "44X72"
    assert identities["US_REAL_RETAIL_SALES"].data_type_code == "SM"


def test_census_live_wire_values_and_response_windows_are_explicit() -> None:
    catalog = load_source_mapping_catalog_for("census")

    for definition in catalog.mappings:
        identity = CensusMappingIdentity.model_validate(definition.source_parameters)
        assert identity.seasonally_adj == "yes"
        assert identity.geo_level_code == "US"
        assert identity.geography_query == "us:*"
        assert identity.identity_fields == ["program_code"]
        assert identity.request_year_batch_size == (1 if identity.dataset == "m3" else 5)
