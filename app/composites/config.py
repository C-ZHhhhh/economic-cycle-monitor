"""Load and validate the unique Stage 3D macro-state authority."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from app.composites.schemas import (
    BreadthMethod,
    CompositeCatalog,
    DispersionMethod,
    GrowthAggregationMethod,
    GrowthConfidenceMethod,
    StateArchitecture,
)
from app.pillars.config import load_pillar_catalog
from app.pillars.schemas import PillarCatalog, PillarRole

DEFAULT_COMPOSITE_CONFIG_PATH = Path("config/composites/us_macro_state.yaml")
EXPECTED_STATE_ID = "US_MACRO__STATE_VECTOR"
EXPECTED_GROWTH_ID = "US_MACRO__GROWTH_STRENGTH"
EXPECTED_GROWTH_PILLARS = [
    "US_MACRO__REAL_ACTIVITY_AND_DEMAND",
    "US_MACRO__LABOR_STRENGTH",
    "US_MACRO__MANUFACTURING_AND_INVESTMENT",
    "US_MACRO__HOUSING_STRENGTH",
]
EXPECTED_INFLATION_PILLAR = "US_MACRO__INFLATION_PRESSURE"
EXPECTED_CONTEXT_PILLARS = [
    "US_MACRO__HOUSEHOLD_FINANCIAL_CUSHION",
    "US_MACRO__CREDIT_USAGE_PRESSURE",
    "US_MACRO__INVENTORY_ACCUMULATION_PRESSURE",
    "US_MACRO__INVENTORY_ABSORPTION_STRENGTH",
]


def _hash_payload(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_composite_catalog(
    path: Path = DEFAULT_COMPOSITE_CONFIG_PATH,
    *,
    pillar_catalog: PillarCatalog | None = None,
) -> CompositeCatalog:
    """Load, bind, validate, and hash the Stage 3D-v1 state specification."""
    pillars = pillar_catalog or load_pillar_catalog()
    raw_value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw_value, dict):
        raise ValueError("Composite configuration must be a mapping")
    raw: dict[str, Any] = raw_value
    if raw.get("composite_spec_version") != "stage3d-v1":
        raise ValueError("COMPOSITE_SPEC_VERSION_MISMATCH")
    bindings = {
        "required_source_registry_version": pillars.required_source_registry_version,
        "required_source_registry_hash": pillars.required_source_registry_hash,
        "required_feature_spec_version": pillars.required_feature_spec_version,
        "required_normalization_spec_version": pillars.required_normalization_spec_version,
        "required_normalization_spec_hash": pillars.required_normalization_spec_hash,
        "required_pillar_spec_version": pillars.pillar_spec_version,
        "required_pillar_spec_hash": pillars.specification_hash,
    }
    for key, expected in bindings.items():
        if raw.get(key) != expected:
            raise ValueError(f"COMPOSITE_BINDING_MISMATCH:{key}")

    payload = {**raw, "source_path": path, "specification_hash": "pending"}
    catalog = CompositeCatalog.model_validate(payload)
    if catalog.architecture is not StateArchitecture.THREE_LAYER_STATE:
        raise ValueError("UNSUPPORTED_STATE_ARCHITECTURE")
    if catalog.state_id != EXPECTED_STATE_ID:
        raise ValueError("UNSTABLE_STATE_ID")
    if catalog.growth.component_id != EXPECTED_GROWTH_ID:
        raise ValueError("UNSTABLE_GROWTH_ID")
    if catalog.growth.aggregation_method is not GrowthAggregationMethod.EQUAL_WEIGHT_MEAN:
        raise ValueError("UNSUPPORTED_GROWTH_AGGREGATION")
    if (
        catalog.growth.confidence_method
        is not GrowthConfidenceMethod.EXPECTED_BASE_WEIGHTED_PILLAR_CONFIDENCE
    ):
        raise ValueError("UNSUPPORTED_GROWTH_CONFIDENCE")
    if catalog.growth.breadth_method is not BreadthMethod.SIGNED_BREADTH:
        raise ValueError("UNSUPPORTED_BREADTH_METHOD")
    if catalog.growth.dispersion_method is not DispersionMethod.POPULATION_STD:
        raise ValueError("UNSUPPORTED_DISPERSION_METHOD")
    if catalog.growth.minimum_growth_pillars != 3:
        raise ValueError("GROWTH_QUORUM_DRIFT")
    configured_growth = [item.pillar_id for item in catalog.growth.pillars]
    if configured_growth != EXPECTED_GROWTH_PILLARS:
        raise ValueError("GROWTH_PILLAR_INVENTORY_DRIFT")
    if any(item.base_weight != Decimal("0.25") for item in catalog.growth.pillars):
        raise ValueError("GROWTH_BASE_WEIGHT_DRIFT")
    if catalog.inflation_pillar_id != EXPECTED_INFLATION_PILLAR:
        raise ValueError("INFLATION_REFERENCE_DRIFT")
    if catalog.context_pillar_ids != EXPECTED_CONTEXT_PILLARS:
        raise ValueError("CONTEXT_PILLAR_INVENTORY_DRIFT")

    by_pillar = pillars.by_pillar()
    expected_all = {
        *EXPECTED_GROWTH_PILLARS,
        EXPECTED_INFLATION_PILLAR,
        *EXPECTED_CONTEXT_PILLARS,
    }
    if set(by_pillar) != expected_all or len(by_pillar) != 9:
        raise ValueError("STAGE3C_PILLAR_INVENTORY_DRIFT")
    if any(
        by_pillar[pillar_id].pillar_role is not PillarRole.CORE
        or not by_pillar[pillar_id].core_composite_eligible
        for pillar_id in EXPECTED_GROWTH_PILLARS
    ):
        raise ValueError("GROWTH_PILLAR_ROLE_DRIFT")
    inflation = by_pillar[EXPECTED_INFLATION_PILLAR]
    if inflation.pillar_role is not PillarRole.CORE or not inflation.core_composite_eligible:
        raise ValueError("INFLATION_PILLAR_ROLE_DRIFT")
    if any(
        by_pillar[pillar_id].pillar_role is not PillarRole.CONTEXTUAL
        or by_pillar[pillar_id].core_composite_eligible
        for pillar_id in EXPECTED_CONTEXT_PILLARS
    ):
        raise ValueError("CONTEXT_PILLAR_ROLE_DRIFT")

    hash_payload = raw.copy()
    return CompositeCatalog.model_validate(
        {
            **raw,
            "source_path": path,
            "specification_hash": _hash_payload(hash_payload),
        }
    )
