"""Offline validation for the stage3a-v1 feature specification authority."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.features.config import DEFAULT_FEATURE_CONFIG_PATH, load_feature_catalog
from app.features.formulas import FORMULA_REGISTRY_VERSION
from app.features.schemas import FormulaId
from app.source_registry.config import load_source_registry

EXPECTED_PRIMARY = {
    "US_REAL_GDP": "GROWTH_QOQ_ANNUALIZED",
    "US_REAL_PCE": "GROWTH_3M_ANNUALIZED",
    "US_REAL_DISPOSABLE_INCOME": "GROWTH_3M_ANNUALIZED",
    "US_INDUSTRIAL_PRODUCTION": "GROWTH_3M_ANNUALIZED",
    "US_REAL_RETAIL_SALES": "GROWTH_3M_ANNUALIZED",
    "US_NONFARM_EMPLOYMENT": "CHANGE_1M",
    "US_CPI": "INFLATION_3M_ANNUALIZED",
    "US_CORE_CPI": "INFLATION_3M_ANNUALIZED",
    "US_PCE_PRICE": "INFLATION_3M_ANNUALIZED",
    "US_CORE_PCE_PRICE": "INFLATION_3M_ANNUALIZED",
    "US_PPI_COMMODITIES": "INFLATION_YOY",
    "US_UNEMPLOYMENT_RATE": "LEVEL",
    "US_U6_RATE": "LEVEL",
    "US_PARTICIPATION_RATE": "CHANGE_3M_PP",
    "US_EMPLOYMENT_POP_RATIO": "CHANGE_3M_PP",
    "US_INITIAL_CLAIMS": "MA_4W",
    "US_CONTINUING_CLAIMS": "MA_4W",
    "US_JOB_OPENINGS": "MA_3M",
    "US_HIRES": "LEVEL",
    "US_QUITS": "LEVEL",
    "US_TEMP_HELP_EMPLOYMENT": "GROWTH_3M_ANNUALIZED",
    "US_MANUFACTURING_HOURS": "LEVEL",
    "US_AVERAGE_HOURLY_EARNINGS": "GROWTH_3M_ANNUALIZED",
    "US_SAVING_RATE": "LEVEL",
    "US_CONSUMER_CREDIT": "GROWTH_3M_ANNUALIZED",
    "US_REVOLVING_CREDIT": "GROWTH_3M_ANNUALIZED",
    "US_DURABLE_GOODS_ORDERS": "MA3_BLOCK_GROWTH_ANNUALIZED",
    "US_CORE_CAPITAL_GOODS_ORDERS": "MA3_BLOCK_GROWTH_ANNUALIZED",
    "US_MANUFACTURING_CAPACITY": "LEVEL",
    "US_HOUSING_STARTS": "MA3_BLOCK_GROWTH_ANNUALIZED",
    "US_BUILDING_PERMITS": "MA3_BLOCK_GROWTH_ANNUALIZED",
    "US_BUSINESS_INVENTORIES": "GROWTH_YOY",
    "US_INVENTORY_SALES_RATIO": "LEVEL",
}


def _raw() -> dict[str, Any]:
    value = yaml.safe_load(DEFAULT_FEATURE_CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _indicator(raw: dict[str, Any], indicator_id: str) -> dict[str, Any]:
    return next(item for item in raw["indicators"] if item["indicator_id"] == indicator_id)


def _mutated(tmp_path: Path, mutation: Callable[[dict[str, Any]], None]) -> Path:
    raw = _raw()
    mutation(raw)
    path = tmp_path / "features.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_feature_catalog_is_complete_and_bound_to_frozen_registry() -> None:
    registry = load_source_registry()
    catalog = load_feature_catalog(source_registry=registry)
    assert catalog.feature_spec_version == "stage3a-v1"
    assert catalog.formula_registry_version == FORMULA_REGISTRY_VERSION
    assert catalog.required_source_registry_version == "stage2b5-v3"
    assert catalog.required_source_registry_hash == registry.registry_hash
    assert len(catalog.indicators) == len(registry.policies) == 33
    assert set(catalog.by_indicator()) == set(registry.by_indicator())


def test_feature_counts_and_global_id_uniqueness_are_frozen() -> None:
    catalog = load_feature_catalog()
    features = [feature for item in catalog.indicators for feature in item.features]
    assert len(features) == 123
    assert sum(item.role == "primary" for item in features) == 33
    assert sum(item.role == "supporting" for item in features) == 90
    assert len({item.feature_id for item in features}) == len(features)


@pytest.mark.parametrize(("indicator_id", "suffix"), sorted(EXPECTED_PRIMARY.items()))
def test_every_approved_primary_feature_is_exact(indicator_id: str, suffix: str) -> None:
    spec = load_feature_catalog().by_indicator()[indicator_id]
    primary = [item for item in spec.features if item.role == "primary"]
    assert [item.feature_id for item in primary] == [f"{indicator_id}__{suffix}"]


def test_feature_ids_are_stable_namespaced_and_descriptive() -> None:
    catalog = load_feature_catalog()
    for indicator in catalog.indicators:
        for feature in indicator.features:
            assert feature.feature_id.startswith(f"{indicator.indicator_id}__")
            assert feature.display_name
            assert feature.description
            assert feature.feature_id.upper() == feature.feature_id


def test_inflation_horizons_and_ppi_nsa_decision_are_exact() -> None:
    by_indicator = load_feature_catalog().by_indicator()
    for indicator_id in ("US_CPI", "US_CORE_CPI", "US_PCE_PRICE", "US_CORE_PCE_PRICE"):
        suffixes = {
            item.feature_id.split("__", 1)[1] for item in by_indicator[indicator_id].features
        }
        assert suffixes == {
            "INFLATION_1M_ANNUALIZED",
            "INFLATION_3M_ANNUALIZED",
            "INFLATION_6M_ANNUALIZED",
            "INFLATION_YOY",
        }
    ppi = by_indicator["US_PPI_COMMODITIES"]
    assert (
        next(item for item in ppi.features if item.role == "primary").formula_id
        is FormulaId.MONTHLY_YOY
    )
    assert all("ANNUALIZED" not in item.feature_id for item in ppi.features)


def test_claims_supporting_windows_are_4_13_and_52_weeks() -> None:
    by_indicator = load_feature_catalog().by_indicator()
    for indicator_id in ("US_INITIAL_CLAIMS", "US_CONTINUING_CLAIMS"):
        values = {(item.formula_id, item.window) for item in by_indicator[indicator_id].features}
        assert values == {
            (FormulaId.MOVING_AVERAGE_K, 4),
            (FormulaId.MA4W_CHANGE_KW, 4),
            (FormulaId.MA4W_CHANGE_KW, 13),
            (FormulaId.MA4W_YOY_PCT, 52),
        }


def test_hires_and_quits_use_rate_points_not_percentage_points() -> None:
    by_indicator = load_feature_catalog().by_indicator()
    for indicator_id in ("US_HIRES", "US_QUITS"):
        changes = [
            item
            for item in by_indicator[indicator_id].features
            if item.formula_id is FormulaId.POINT_DIFF_K
        ]
        assert changes
        assert {item.output_unit for item in changes} == {"rate_points"}
        assert all("PERCENTAGE" not in item.feature_id for item in changes)


def test_config_contains_no_provider_selection_scoring_or_expression_fields() -> None:
    forbidden = {
        "provider",
        "source",
        "primary_source",
        "fallback_source",
        "weight",
        "direction",
        "normalization",
        "expression",
        "python",
    }

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for item in value.values() for key in keys(item)}
        if isinstance(value, list):
            return {key for item in value for key in keys(item)}
        return set()

    assert not (keys(_raw()) & forbidden)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw.update(required_source_registry_hash="0" * 64), "REGISTRY_HASH_MISMATCH"),
        (
            lambda raw: raw.update(required_source_registry_version="old"),
            "REGISTRY_VERSION_MISMATCH",
        ),
        (
            lambda raw: raw.update(formula_registry_version="old"),
            "FORMULA_REGISTRY_VERSION_MISMATCH",
        ),
        (lambda raw: raw["indicators"].pop(), "FEATURE_INVENTORY_DRIFT"),
        (lambda raw: raw["indicators"].append(raw["indicators"][0]), "Feature indicator IDs"),
    ],
)
def test_catalog_drift_fails_closed(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        load_feature_catalog(_mutated(tmp_path, mutation))


def test_duplicate_feature_id_fails_closed(tmp_path: Path) -> None:
    def mutation(raw: dict[str, Any]) -> None:
        raw["indicators"][1]["features"][0]["feature_id"] = raw["indicators"][0]["features"][0][
            "feature_id"
        ]

    with pytest.raises(ValueError, match="globally unique"):
        load_feature_catalog(_mutated(tmp_path, mutation))


@pytest.mark.parametrize("role", ["supporting", "primary"])
def test_exactly_one_primary_is_required(tmp_path: Path, role: str) -> None:
    def mutation(raw: dict[str, Any]) -> None:
        features = raw["indicators"][0]["features"]
        if role == "supporting":
            features[0]["role"] = "supporting"
        else:
            features[1]["role"] = "primary"

    with pytest.raises(ValidationError, match="exactly one primary"):
        load_feature_catalog(_mutated(tmp_path, mutation))


def test_unknown_formula_and_arbitrary_expression_are_rejected(tmp_path: Path) -> None:
    def unknown(raw: dict[str, Any]) -> None:
        raw["indicators"][0]["features"][0]["formula_id"] = "EVAL_EXPRESSION"
        raw["indicators"][0]["features"][0]["expression"] = "x * 100"

    with pytest.raises(ValidationError):
        load_feature_catalog(_mutated(tmp_path, unknown))


@pytest.mark.parametrize("indicator_id", ["US_HIRES", "US_QUITS", "US_INVENTORY_SALES_RATIO"])
def test_rate_and_ratio_percent_growth_is_forbidden(tmp_path: Path, indicator_id: str) -> None:
    def mutation(raw: dict[str, Any]) -> None:
        feature = _indicator(raw, indicator_id)["features"][0]
        feature.update(formula_id="MONTHLY_YOY", window=12, output_unit="percent")

    with pytest.raises(ValueError, match="Compound growth is forbidden"):
        load_feature_catalog(_mutated(tmp_path, mutation))


def test_formula_frequency_incompatibility_is_rejected(tmp_path: Path) -> None:
    def mutation(raw: dict[str, Any]) -> None:
        feature = _indicator(raw, "US_REAL_GDP")["features"][0]
        feature.update(formula_id="MONTHLY_YOY", window=12, output_unit="percent")

    with pytest.raises(ValueError, match="incompatible with quarterly"):
        load_feature_catalog(_mutated(tmp_path, mutation))


def test_unsupported_output_unit_is_rejected(tmp_path: Path) -> None:
    def mutation(raw: dict[str, Any]) -> None:
        _indicator(raw, "US_CPI")["features"][0]["output_unit"] = "score"

    with pytest.raises(ValueError, match="output unit"):
        load_feature_catalog(_mutated(tmp_path, mutation))


@pytest.mark.parametrize(
    ("indicator_id", "feature_index", "window", "message"),
    [
        ("US_REAL_GDP", 0, 2, "window=1"),
        ("US_CPI", 3, 11, "window=12"),
        ("US_INITIAL_CLAIMS", 3, 51, "window=52"),
        ("US_CPI", 0, 5, "divide 12"),
    ],
)
def test_formula_window_contract_is_rejected(
    tmp_path: Path,
    indicator_id: str,
    feature_index: int,
    window: int,
    message: str,
) -> None:
    def mutation(raw: dict[str, Any]) -> None:
        _indicator(raw, indicator_id)["features"][feature_index]["window"] = window

    with pytest.raises(ValueError, match=message):
        load_feature_catalog(_mutated(tmp_path, mutation))


def test_feature_id_must_remain_under_stable_indicator_namespace(tmp_path: Path) -> None:
    def mutation(raw: dict[str, Any]) -> None:
        _indicator(raw, "US_CPI")["features"][0]["feature_id"] = "RENAMED_FEATURE"

    with pytest.raises(ValueError, match="namespaced by US_CPI"):
        load_feature_catalog(_mutated(tmp_path, mutation))


def test_specification_hash_is_deterministic() -> None:
    assert load_feature_catalog().specification_hash == load_feature_catalog().specification_hash
