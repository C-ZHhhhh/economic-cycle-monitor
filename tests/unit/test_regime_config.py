"""Stage 3E authority, taxonomy, and fail-closed binding tests."""

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from app.regimes.config import (
    DEFAULT_REGIME_CONFIG_PATH,
    EXPECTED_REGIME_IDS,
    load_regime_catalog,
)
from app.regimes.schemas import (
    BaseTaxonomy,
    ContextPolicy,
    EvaluationCadence,
    HysteresisPolicy,
    RegimeArchitecture,
    TrajectoryMethod,
)


def _mutated_config(tmp_path: Path, mutate: object) -> Path:
    raw = yaml.safe_load(DEFAULT_REGIME_CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert callable(mutate)
    mutate(raw)
    target = tmp_path / "regime.yaml"
    target.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return target


def test_catalog_binds_every_frozen_upstream_authority() -> None:
    catalog = load_regime_catalog()
    assert catalog.regime_spec_version == "stage3e-v1"
    assert catalog.required_stage3d_spec_version == "stage3d-v1"
    assert catalog.required_stage3d_spec_hash == (
        "8be464960ae16c9ce24f79f99fd13708dcb9be996ab5e8dbdcdc9f14880269c4"
    )
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
    assert load_regime_catalog().specification_hash == catalog.specification_hash


def test_architecture_and_policies_are_frozen() -> None:
    catalog = load_regime_catalog()
    assert catalog.architecture is RegimeArchitecture.LEVEL_PLUS_TRAJECTORY
    assert catalog.base_taxonomy is BaseTaxonomy.HIERARCHICAL_3X3
    assert catalog.evaluation_cadence is EvaluationCadence.ANY_EOD_STATELESS
    assert catalog.hysteresis_policy is HysteresisPolicy.NONE
    assert catalog.context_policy is ContextPolicy.PASS_THROUGH_ONLY
    assert catalog.trajectory.method is TrajectoryMethod.DELTA_3M_REAL_TIME_PATH
    assert catalog.trajectory.horizon_months == 3


def test_thresholds_ratios_and_margin_are_exact() -> None:
    catalog = load_regime_catalog()
    for axis in (catalog.growth, catalog.inflation):
        assert str(axis.lower_threshold) == "-0.10"
        assert str(axis.upper_threshold) == "0.10"
        assert axis.minimum_confidence.numerator == 2
        assert axis.minimum_confidence.denominator == 3
        assert axis.minimum_confidence.accepts(axis.minimum_confidence.value)
        assert axis.minimum_confidence.accepts(Decimal(2) / Decimal(3))
        assert not axis.minimum_confidence.accepts(Decimal("0.6666666666666666666666666666"))
    assert catalog.trajectory.lower_threshold == Decimal("-0.10")
    assert catalog.trajectory.upper_threshold == Decimal("0.10")
    assert catalog.classification_margin.warning_threshold == Decimal("0.05")


def test_taxonomy_contains_exactly_the_nine_stable_mechanical_ids() -> None:
    catalog = load_regime_catalog()
    assert [item.regime_id for item in catalog.base_regimes] == EXPECTED_REGIME_IDS
    assert len(catalog.base_regimes) == len(catalog.by_conditions()) == 9
    assert len({item.display_name for item in catalog.base_regimes}) == 9


def test_authority_contains_no_forbidden_product_semantics() -> None:
    text = DEFAULT_REGIME_CONFIG_PATH.read_text(encoding="utf-8").lower()
    forbidden = [
        "recession",
        "expansion",
        "recovery",
        "stagflation",
        "reflation",
        "goldilocks",
        "nber",
        "portfolio",
        "market timing",
        "llm",
    ]
    assert not any(term in text for term in forbidden)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.update(regime_spec_version="stage3e-v2"),
        lambda raw: raw.update(required_stage3d_spec_hash="wrong"),
        lambda raw: raw.update(required_source_registry_hash="wrong"),
        lambda raw: raw.update(required_feature_spec_version="wrong"),
        lambda raw: raw.update(required_normalization_spec_hash="wrong"),
        lambda raw: raw.update(required_pillar_spec_hash="wrong"),
        lambda raw: raw.update(architecture="LEVEL_ONLY"),
        lambda raw: raw.update(base_taxonomy="QUADRANT_2X2"),
        lambda raw: raw.update(evaluation_cadence="MONTH_END"),
        lambda raw: raw.update(hysteresis_policy="PERSISTENCE"),
        lambda raw: raw["growth"].update(lower_threshold="-0.20"),
        lambda raw: raw["inflation"].update(upper_threshold="0.20"),
        lambda raw: raw["growth"]["minimum_confidence"].update(numerator=1),
        lambda raw: raw["trajectory"].update(horizon_months=1),
        lambda raw: raw["trajectory"].update(method="SLOPE"),
        lambda raw: raw["classification_margin"].update(warning_threshold="0.10"),
        lambda raw: raw["base_regimes"].pop(),
        lambda raw: raw["base_regimes"][0].update(regime_id="RENAMED"),
        lambda raw: raw.update(context_policy="MODIFIES_BASE"),
        lambda raw: raw.update(arbitrary_expression="growth > 0"),
    ],
)
def test_configuration_drift_fails_closed(tmp_path: Path, mutate: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        load_regime_catalog(_mutated_config(tmp_path, mutate))
