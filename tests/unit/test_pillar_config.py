"""Stage 3C pillar taxonomy, binding, and fail-closed configuration tests."""

from pathlib import Path

import pytest
import yaml

from app.pillars.config import DEFAULT_PILLAR_CONFIG_PATH, load_pillar_catalog
from app.pillars.schemas import PillarRole, SupportingUsage
from app.signals.config import load_signal_catalog
from app.signals.schemas import Direction

EXPECTED_PILLARS = {
    "US_MACRO__REAL_ACTIVITY_AND_DEMAND": 4,
    "US_MACRO__LABOR_STRENGTH": 11,
    "US_MACRO__INFLATION_PRESSURE": 6,
    "US_MACRO__MANUFACTURING_AND_INVESTMENT": 5,
    "US_MACRO__HOUSING_STRENGTH": 2,
    "US_MACRO__HOUSEHOLD_FINANCIAL_CUSHION": 1,
    "US_MACRO__CREDIT_USAGE_PRESSURE": 2,
    "US_MACRO__INVENTORY_ACCUMULATION_PRESSURE": 1,
    "US_MACRO__INVENTORY_ABSORPTION_STRENGTH": 1,
}


def _mutated_config(tmp_path: Path, mutate: object) -> Path:
    raw = yaml.safe_load(DEFAULT_PILLAR_CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert callable(mutate)
    mutate(raw)
    target = tmp_path / "pillars.yaml"
    target.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return target


def test_catalog_has_frozen_versions_hashes_and_inventory() -> None:
    catalog = load_pillar_catalog()
    assert catalog.pillar_spec_version == "stage3c-v1"
    assert catalog.required_source_registry_version == "stage2b5-v3"
    assert catalog.required_source_registry_hash == (
        "7a13095907733a1d6092ef5231c67de7528886c7eda9eeb73ed2166764834758"
    )
    assert catalog.required_feature_spec_version == "stage3a-v1"
    assert catalog.required_normalization_spec_version == "stage3b-v1"
    assert catalog.required_normalization_spec_hash == (
        "89ec106423a29a8fc477fbc4797f9b8e85dc3863b50e031852b302a49b58b173"
    )
    assert catalog.expected_signal_count == 123
    assert catalog.expected_primary_count == 33
    assert catalog.expected_supporting_count == 90
    assert len(catalog.specification_hash) == 64


def test_nine_pillar_taxonomy_and_roles_are_exact() -> None:
    catalog = load_pillar_catalog()
    assert {item.pillar_id: len(item.primary_signal_ids) for item in catalog.pillars} == (
        EXPECTED_PILLARS
    )
    assert sum(item.pillar_role is PillarRole.CORE for item in catalog.pillars) == 5
    contextual = [item for item in catalog.pillars if item.pillar_role is PillarRole.CONTEXTUAL]
    assert len(contextual) == 4
    assert all(not item.core_composite_eligible for item in contextual)


def test_all_primary_and_supporting_signals_are_bound_once() -> None:
    pillars = load_pillar_catalog()
    signals = load_signal_catalog()
    primary = [signal for pillar in pillars.pillars for signal in pillar.primary_signal_ids]
    supporting = [
        signal.signal_id for pillar in pillars.pillars for signal in pillar.supporting_signals
    ]
    assert len(primary) == len(set(primary)) == 33
    assert len(supporting) == len(set(supporting)) == 90
    assert set(primary) == {
        item.signal_id for item in signals.signals if item.feature_role == "primary"
    }
    assert set(supporting) == {
        item.signal_id for item in signals.signals if item.feature_role == "supporting"
    }


def test_supporting_bindings_preserve_pillar_and_redundancy_identity() -> None:
    catalog = load_pillar_catalog()
    signals = load_signal_catalog().by_signal()
    for pillar in catalog.pillars:
        for binding in pillar.supporting_signals:
            assert binding.related_pillar_id == pillar.pillar_id
            assert binding.redundancy_group == signals[binding.signal_id].redundancy_group


def test_context_only_signal_is_never_directional_or_numeric() -> None:
    catalog = load_pillar_catalog()
    signals = load_signal_catalog().by_signal()
    bindings = {
        item.signal_id: item for pillar in catalog.pillars for item in pillar.supporting_signals
    }
    context_ids = {
        item.signal_id
        for item in signals.values()
        if item.direction is Direction.NEUTRAL_CONTEXT_ONLY
    }
    assert context_ids == {"US_PPI_COMMODITIES__LEVEL__INFLATION_PRESSURE__SIGNAL"}
    assert all(bindings[item].intended_usage is SupportingUsage.NOT_USED_V1 for item in context_ids)
    assert context_ids.isdisjoint(
        signal for pillar in catalog.pillars for signal in pillar.primary_signal_ids
    )


def test_participation_and_ahe_keep_equal_indicator_primary_membership() -> None:
    by_pillar = load_pillar_catalog().by_pillar()
    labor = by_pillar["US_MACRO__LABOR_STRENGTH"].primary_signal_ids
    inflation = by_pillar["US_MACRO__INFLATION_PRESSURE"].primary_signal_ids
    assert any(item.startswith("US_PARTICIPATION_RATE__") for item in labor)
    assert len(labor) == 11
    assert any(item.startswith("US_AVERAGE_HOURLY_EARNINGS__") for item in inflation)
    assert len(inflation) == 6


def test_only_labor_and_inflation_retain_concentration_warning() -> None:
    warned = {
        item.pillar_id for item in load_pillar_catalog().pillars if item.concentration_warning
    }
    assert warned == {
        "US_MACRO__LABOR_STRENGTH",
        "US_MACRO__INFLATION_PRESSURE",
    }


@pytest.mark.parametrize(
    ("pillar_id", "minimum_valid"),
    [
        ("US_MACRO__REAL_ACTIVITY_AND_DEMAND", 3),
        ("US_MACRO__LABOR_STRENGTH", 8),
        ("US_MACRO__INFLATION_PRESSURE", 4),
        ("US_MACRO__MANUFACTURING_AND_INVESTMENT", 4),
        ("US_MACRO__HOUSING_STRENGTH", 2),
        ("US_MACRO__HOUSEHOLD_FINANCIAL_CUSHION", 1),
        ("US_MACRO__CREDIT_USAGE_PRESSURE", 2),
        ("US_MACRO__INVENTORY_ACCUMULATION_PRESSURE", 1),
        ("US_MACRO__INVENTORY_ABSORPTION_STRENGTH", 1),
    ],
)
def test_minimum_valid_counts_are_derived_from_inventory(
    pillar_id: str, minimum_valid: int
) -> None:
    catalog = load_pillar_catalog()
    count = len(catalog.by_pillar()[pillar_id].primary_signal_ids)
    policy = catalog.coverage_policy
    derived = next(
        value
        for value in range(count + 1)
        if value * policy.minimum_denominator >= count * policy.minimum_numerator
    )
    assert derived == minimum_valid


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw.update(expected_signal_count=122), "EXPECTED_SIGNAL_INVENTORY"),
        (lambda raw: raw.update(required_source_registry_hash="wrong"), "REGISTRY_HASH"),
        (
            lambda raw: raw.update(required_normalization_spec_hash="wrong"),
            "NORMALIZATION_SPEC_HASH",
        ),
        (lambda raw: raw.update(aggregation_method="AXIS_FIRST"), "UNSUPPORTED_AGGREGATION"),
        (
            lambda raw: raw.update(contribution_mapping="LINEAR"),
            "UNSUPPORTED_CONTRIBUTION_MAPPING",
        ),
        (
            lambda raw: raw["contribution_mapping_parameters"].update(scale="0"),
            "TANH_PARAMETER_DRIFT",
        ),
        (
            lambda raw: raw["contribution_mapping_parameters"].update(scale="3"),
            "TANH_PARAMETER_DRIFT",
        ),
        (
            lambda raw: raw["coverage_policy"].update(minimum_numerator=4, minimum_denominator=3),
            "cannot exceed one",
        ),
        (
            lambda raw: raw["coverage_policy"].update(minimum_numerator=3, minimum_denominator=4),
            "COVERAGE_POLICY_DRIFT",
        ),
        (
            lambda raw: raw["pillars"][5].update(core_composite_eligible=True),
            "Contextual pillars",
        ),
        (
            lambda raw: raw["pillars"][0].update(pillar_id="US_MACRO__RENAMED"),
            "PILLAR_TAXONOMY_DRIFT",
        ),
        (
            lambda raw: raw["pillars"][1]["primary_indicator_ids"].append("US_REAL_GDP"),
            "more than one pillar",
        ),
        (
            lambda raw: raw["pillars"][0]["primary_indicator_ids"].pop(),
            "PRIMARY_MAPPING_DRIFT",
        ),
        (
            lambda raw: raw["supporting_usage_overrides"].update(UNKNOWN="CONFIRMATION"),
            "UNKNOWN_SUPPORTING_SIGNALS",
        ),
        (
            lambda raw: raw["supporting_usage_overrides"].update(
                US_PPI_COMMODITIES__LEVEL__INFLATION_PRESSURE__SIGNAL="CONFIRMATION"
            ),
            "Context-only",
        ),
    ],
)
def test_configuration_drift_fails_closed(tmp_path: Path, mutate: object, message: str) -> None:
    path = _mutated_config(tmp_path, mutate)
    with pytest.raises((ValueError, TypeError), match=message):
        load_pillar_catalog(path)
