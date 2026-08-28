"""Stage 3D authority, inventory, and fail-closed binding tests."""

from pathlib import Path

import pytest
import yaml

from app.composites.config import (
    DEFAULT_COMPOSITE_CONFIG_PATH,
    EXPECTED_CONTEXT_PILLARS,
    EXPECTED_GROWTH_PILLARS,
    EXPECTED_INFLATION_PILLAR,
    load_composite_catalog,
)
from app.composites.schemas import (
    BreadthMethod,
    DispersionMethod,
    GrowthAggregationMethod,
    GrowthConfidenceMethod,
    StateArchitecture,
)


def _mutated_config(tmp_path: Path, mutate: object) -> Path:
    raw = yaml.safe_load(DEFAULT_COMPOSITE_CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert callable(mutate)
    mutate(raw)
    target = tmp_path / "composite.yaml"
    target.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return target


def test_catalog_binds_every_frozen_upstream_authority() -> None:
    catalog = load_composite_catalog()
    assert catalog.composite_spec_version == "stage3d-v1"
    assert catalog.required_source_registry_version == "stage2b5-v3"
    assert catalog.required_source_registry_hash == (
        "7a13095907733a1d6092ef5231c67de7528886c7eda9eeb73ed2166764834758"
    )
    assert catalog.required_feature_spec_version == "stage3a-v1"
    assert catalog.required_normalization_spec_version == "stage3b-v1"
    assert catalog.required_normalization_spec_hash == (
        "89ec106423a29a8fc477fbc4797f9b8e85dc3863b50e031852b302a49b58b173"
    )
    assert catalog.required_pillar_spec_version == "stage3c-v1"
    assert catalog.required_pillar_spec_hash == (
        "fed386805496d63cc7dd1ed0b6f772a8f821042fc9b04d93884d6c008f6b3d41"
    )
    assert len(catalog.specification_hash) == 64
    assert load_composite_catalog().specification_hash == catalog.specification_hash


def test_three_layer_state_has_stable_ids_without_inflation_alias() -> None:
    catalog = load_composite_catalog()
    assert catalog.architecture is StateArchitecture.THREE_LAYER_STATE
    assert catalog.state_id == "US_MACRO__STATE_VECTOR"
    assert catalog.growth.component_id == "US_MACRO__GROWTH_STRENGTH"
    assert catalog.inflation_pillar_id == EXPECTED_INFLATION_PILLAR
    assert catalog.inflation_pillar_id not in {item.pillar_id for item in catalog.growth.pillars}


def test_growth_inventory_weights_and_methods_are_exact() -> None:
    growth = load_composite_catalog().growth
    assert [item.pillar_id for item in growth.pillars] == EXPECTED_GROWTH_PILLARS
    assert {item.base_weight for item in growth.pillars} == {0.25}
    assert sum(item.base_weight for item in growth.pillars) == 1
    assert growth.minimum_growth_pillars == 3
    assert growth.aggregation_method is GrowthAggregationMethod.EQUAL_WEIGHT_MEAN
    assert (
        growth.confidence_method is GrowthConfidenceMethod.EXPECTED_BASE_WEIGHTED_PILLAR_CONFIDENCE
    )
    assert growth.breadth_method is BreadthMethod.SIGNED_BREADTH
    assert growth.dispersion_method is DispersionMethod.POPULATION_STD


def test_context_is_complete_vector_only() -> None:
    catalog = load_composite_catalog()
    assert catalog.context_pillar_ids == EXPECTED_CONTEXT_PILLARS
    raw = yaml.safe_load(DEFAULT_COMPOSITE_CONFIG_PATH.read_text(encoding="utf-8"))
    assert "context_score" not in raw
    assert "regime" not in raw
    assert "threshold" not in raw
    assert "headline_score" not in raw


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.update(composite_spec_version="stage3d-v2"),
        lambda raw: raw.update(required_source_registry_hash="wrong"),
        lambda raw: raw.update(required_feature_spec_version="wrong"),
        lambda raw: raw.update(required_normalization_spec_hash="wrong"),
        lambda raw: raw.update(required_pillar_spec_hash="wrong"),
        lambda raw: raw.update(architecture="SINGLE_SCALAR"),
        lambda raw: raw.update(state_id="US_MACRO__HEALTH_SCORE"),
        lambda raw: raw["growth"].update(component_id="RENAMED"),
        lambda raw: raw["growth"].update(aggregation_method="CONFIDENCE_WEIGHTED"),
        lambda raw: raw["growth"].update(minimum_growth_pillars=2),
        lambda raw: raw["growth"]["pillars"][0].update(base_weight="0.30"),
        lambda raw: raw["growth"]["pillars"].reverse(),
        lambda raw: raw.update(inflation_pillar_id="US_MACRO__LABOR_STRENGTH"),
        lambda raw: raw["context_pillar_ids"].pop(),
        lambda raw: raw.update(regime_threshold="0.5"),
    ],
)
def test_configuration_drift_fails_closed(tmp_path: Path, mutate: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        load_composite_catalog(_mutated_config(tmp_path, mutate))
