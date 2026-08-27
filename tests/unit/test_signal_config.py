"""Stage 3B signal authority completeness and semantic decision tests."""

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.features.config import load_feature_catalog
from app.signals.config import load_signal_catalog
from app.signals.schemas import Direction, EconomicAxis, NormalizationMethod, SignalCatalog


@pytest.fixture(scope="module")
def catalog() -> SignalCatalog:
    return load_signal_catalog()


def _raw() -> dict[str, Any]:
    raw: object = yaml.safe_load(Path("config/signals/us_macro_signals.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def _write(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "signals.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_signal_inventory_is_exactly_one_to_one_with_all_features(
    catalog: SignalCatalog,
) -> None:
    features = load_feature_catalog().by_feature()
    assert len(catalog.signals) == len(features) == 123
    assert set(catalog.by_feature()) == set(features)
    assert len(catalog.by_signal()) == 123


def test_signal_role_inventory_remains_frozen(catalog: SignalCatalog) -> None:
    assert Counter(item.feature_role for item in catalog.signals) == {
        "primary": 33,
        "supporting": 90,
    }


def test_signal_spec_is_bound_to_frozen_contracts(catalog: SignalCatalog) -> None:
    assert catalog.normalization_spec_version == "stage3b-v1"
    assert catalog.required_feature_spec_version == "stage3a-v1"
    assert catalog.required_source_registry_version == "stage2b5-v3"
    assert (
        catalog.required_source_registry_hash
        == "7a13095907733a1d6092ef5231c67de7528886c7eda9eeb73ed2166764834758"
    )
    assert not catalog.distribution_includes_current


def test_all_production_signals_use_only_rolling_zscore(catalog: SignalCatalog) -> None:
    assert {item.normalization_method for item in catalog.signals} == {
        NormalizationMethod.ROLLING_ZSCORE
    }


@pytest.mark.parametrize(
    "feature_id",
    list(load_feature_catalog().by_feature()),
)
def test_every_feature_has_stable_deterministic_signal_id(
    feature_id: str, catalog: SignalCatalog
) -> None:
    signal = catalog.by_feature()[feature_id]
    assert signal.signal_id == f"{feature_id}__{signal.economic_axis.value}__SIGNAL"
    assert signal.redundancy_group


def test_window_policy_counts(catalog: SignalCatalog) -> None:
    policies = Counter((item.full_window, item.min_history) for item in catalog.signals)
    assert policies[(40, 20)] == 3
    assert policies[(260, 156)] == 8
    assert policies[(60, 36)] > 0
    assert policies[(120, 60)] > 0
    assert sum(policies.values()) == 123


@pytest.mark.parametrize(
    ("indicator", "axis", "direction"),
    [
        ("US_PARTICIPATION_RATE", EconomicAxis.LABOR_PARTICIPATION, Direction.POSITIVE),
        ("US_CONSUMER_CREDIT", EconomicAxis.CREDIT_USAGE_PRESSURE, Direction.POSITIVE),
        ("US_REVOLVING_CREDIT", EconomicAxis.CREDIT_USAGE_PRESSURE, Direction.POSITIVE),
        ("US_AVERAGE_HOURLY_EARNINGS", EconomicAxis.WAGE_PRESSURE, Direction.POSITIVE),
        (
            "US_SAVING_RATE",
            EconomicAxis.HOUSEHOLD_FINANCIAL_CUSHION,
            Direction.POSITIVE,
        ),
        ("US_QUITS", EconomicAxis.LABOR_TIGHTNESS, Direction.POSITIVE),
        (
            "US_BUSINESS_INVENTORIES",
            EconomicAxis.INVENTORY_ACCUMULATION_PRESSURE,
            Direction.POSITIVE,
        ),
        (
            "US_INVENTORY_SALES_RATIO",
            EconomicAxis.INVENTORY_ABSORPTION_STRENGTH,
            Direction.INVERSE,
        ),
        ("US_INITIAL_CLAIMS", EconomicAxis.LABOR_STRENGTH, Direction.INVERSE),
        ("US_CONTINUING_CLAIMS", EconomicAxis.LABOR_STRENGTH, Direction.INVERSE),
        ("US_UNEMPLOYMENT_RATE", EconomicAxis.LABOR_STRENGTH, Direction.INVERSE),
        ("US_U6_RATE", EconomicAxis.LABOR_STRENGTH, Direction.INVERSE),
        ("US_CPI", EconomicAxis.INFLATION_PRESSURE, Direction.POSITIVE),
        ("US_CORE_CPI", EconomicAxis.INFLATION_PRESSURE, Direction.POSITIVE),
        ("US_PCE_PRICE", EconomicAxis.INFLATION_PRESSURE, Direction.POSITIVE),
        ("US_CORE_PCE_PRICE", EconomicAxis.INFLATION_PRESSURE, Direction.POSITIVE),
    ],
)
def test_formal_family_axis_and_direction_decisions(
    indicator: str,
    axis: EconomicAxis,
    direction: Direction,
    catalog: SignalCatalog,
) -> None:
    family = [item for item in catalog.signals if item.indicator_id == indicator]
    assert family
    assert {item.economic_axis for item in family} == {axis}
    assert {item.direction for item in family} == {direction}


def test_price_level_diagnostic_is_explicit_context_only(catalog: SignalCatalog) -> None:
    signal = catalog.by_feature()["US_PPI_COMMODITIES__LEVEL"]
    assert signal.direction is Direction.NEUTRAL_CONTEXT_ONLY
    assert signal.direction_multiplier is None
    assert sum(item.direction is Direction.NEUTRAL_CONTEXT_ONLY for item in catalog.signals) == 1


def test_direction_distribution_is_stable(catalog: SignalCatalog) -> None:
    assert Counter(item.direction for item in catalog.signals) == {
        Direction.POSITIVE: 102,
        Direction.INVERSE: 20,
        Direction.NEUTRAL_CONTEXT_ONLY: 1,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("normalization_spec_version", "stage3b-v2", "NORMALIZATION_SPEC_VERSION_MISMATCH"),
        ("normalizer_registry_version", "future", "NORMALIZER_REGISTRY_VERSION_MISMATCH"),
        ("required_feature_spec_version", "future", "FEATURE_SPEC_MISMATCH"),
        ("required_source_registry_version", "future", "REGISTRY_VERSION_MISMATCH"),
        ("required_source_registry_hash", "0" * 64, "REGISTRY_HASH_MISMATCH"),
        ("distribution_includes_current", True, "exclude the current observation"),
    ],
)
def test_contract_drift_fails_closed(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    raw = _raw()
    raw[field] = value
    with pytest.raises(ValueError, match=message):
        load_signal_catalog(_write(tmp_path, raw))


def test_missing_signal_fails_closed(tmp_path: Path) -> None:
    raw = _raw()
    raw["signals"].pop()
    with pytest.raises(ValueError, match="123"):
        load_signal_catalog(_write(tmp_path, raw))


def test_duplicate_feature_mapping_fails_closed(tmp_path: Path) -> None:
    raw = _raw()
    raw["signals"][-1] = dict(raw["signals"][0])
    with pytest.raises(ValueError, match="One feature"):
        load_signal_catalog(_write(tmp_path, raw))


def test_duplicate_signal_id_fails_closed(tmp_path: Path) -> None:
    raw = _raw()
    raw["signals"][1]["signal_id"] = raw["signals"][0]["signal_id"]
    with pytest.raises(ValueError, match="globally unique"):
        load_signal_catalog(_write(tmp_path, raw))


def test_role_drift_fails_closed(tmp_path: Path) -> None:
    raw = _raw()
    raw["signals"][0]["feature_role"] = "supporting"
    with pytest.raises(ValueError, match="feature role drift"):
        load_signal_catalog(_write(tmp_path, raw))


def test_invalid_window_fails_closed(tmp_path: Path) -> None:
    raw = _raw()
    raw["signals"][0]["full_window"] = 41
    with pytest.raises(ValueError, match="window policy"):
        load_signal_catalog(_write(tmp_path, raw))


def test_min_history_must_be_below_full_window(tmp_path: Path) -> None:
    raw = _raw()
    raw["signals"][0]["min_history"] = 40
    with pytest.raises(ValidationError, match="min_history"):
        load_signal_catalog(_write(tmp_path, raw))


def test_direction_multiplier_mismatch_fails_closed(tmp_path: Path) -> None:
    raw = _raw()
    raw["signals"][0]["direction_multiplier"] = -1
    with pytest.raises(ValidationError, match="direction multiplier"):
        load_signal_catalog(_write(tmp_path, raw))


def test_missing_redundancy_group_fails_closed(tmp_path: Path) -> None:
    raw = _raw()
    raw["signals"][0]["redundancy_group"] = ""
    with pytest.raises(ValueError, match="redundancy metadata"):
        load_signal_catalog(_write(tmp_path, raw))


def test_config_contains_no_scoring_or_provider_authority() -> None:
    raw_text = Path("config/signals/us_macro_signals.yaml").read_text().lower()
    for forbidden in ("weight:", "pillar:", "score:", "provider:", "api_key", "regime:"):
        assert forbidden not in raw_text
