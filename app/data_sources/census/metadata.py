"""Strict identity validation for pending Census EITS candidates."""

from typing import Any

from app.data_sources.census.schemas import (
    CensusMappingIdentity,
    CensusMetadataValidation,
    CensusRow,
)
from app.source_mappings.schemas import SourceMappingDefinition

REQUIRED_VARIABLES = {
    "cell_value",
    "category_code",
    "data_type_code",
    "seasonally_adj",
    "geo_level_code",
    "time",
}


def validate_census_metadata(
    definition: SourceMappingDefinition,
    dataset_metadata: dict[str, Any],
    variables_metadata: dict[str, Any],
    rows: list[CensusRow],
) -> CensusMetadataValidation:
    """Validate candidate dimensions but never promote a pending mapping."""
    result = CensusMetadataValidation()
    identity = CensusMappingIdentity.model_validate(definition.source_parameters)
    if definition.source_dataset != identity.dataset:
        result.errors.append("unexpected_dataset: source_dataset and identity dataset differ")
    dataset_text = str(dataset_metadata).lower()
    if identity.dataset.lower() not in dataset_text:
        result.errors.append("unexpected_dataset: discovery metadata omitted candidate dataset")
    variables = variables_metadata.get("variables", {})
    if not isinstance(variables, dict) or not REQUIRED_VARIABLES.issubset(variables):
        result.errors.append("source_response_partial: variables metadata omitted required fields")

    category_rows = [row for row in rows if row.category_code == identity.category_code]
    if not category_rows:
        result.errors.append(
            f"unexpected_category: expected exact category {identity.category_code}"
        )
    data_type_rows = [row for row in category_rows if row.data_type_code == identity.data_type_code]
    if category_rows and not data_type_rows:
        result.errors.append(
            f"unexpected_data_type: expected exact data type {identity.data_type_code}"
        )
    result.returned_seasonal_values = sorted({row.seasonally_adj for row in data_type_rows})
    result.returned_geography_values = sorted({row.geo_level_code for row in data_type_rows})
    if not result.returned_seasonal_values:
        result.errors.append("unexpected_seasonal_adjustment: no candidate rows returned")
    if not result.returned_geography_values:
        result.errors.append("unexpected_geography: no candidate rows returned")
    if identity.seasonally_adj is None:
        result.warnings.append(
            "seasonally_adj wire value remains pending explicit live review: "
            f"returned {result.returned_seasonal_values}"
        )
    elif identity.seasonally_adj not in result.returned_seasonal_values:
        result.errors.append(
            "unexpected_seasonal_adjustment: configured wire value was not returned"
        )
    if identity.geo_level_code is None:
        result.warnings.append(
            "geo_level_code wire value remains pending explicit live review: "
            f"returned {result.returned_geography_values}"
        )
    elif identity.geo_level_code not in result.returned_geography_values:
        result.errors.append("unexpected_geography: configured wire value was not returned")
    result.identity_confirmed = not result.errors
    if result.errors:
        result.status = "invalid"
    elif definition.mapping_status in {"valid", "valid_with_warning"}:
        result.status = definition.mapping_status
    else:
        result.status = "pending_review"
    if result.identity_confirmed and result.status == "pending_review":
        result.warnings.append(
            "Candidate identity passed offline/runtime checks but remains pending until V10 review"
        )
    return result
