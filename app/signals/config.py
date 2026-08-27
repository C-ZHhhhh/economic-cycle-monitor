"""Load and validate the unique Stage 3B signal specification authority."""

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.features.config import load_feature_catalog
from app.features.schemas import FeatureCatalog, FormulaId
from app.signals.normalizers import NORMALIZER_REGISTRY_VERSION
from app.signals.schemas import SignalCatalog, SignalDefinition
from app.source_registry.config import load_source_registry
from app.source_registry.schemas import SourceRegistryCatalog

DEFAULT_SIGNAL_CONFIG_PATH = Path("config/signals/us_macro_signals.yaml")
EXPECTED_SIGNAL_COUNT = 123
EXPECTED_PRIMARY_COUNT = 33
EXPECTED_SUPPORTING_COUNT = 90


def expected_window_policy(frequency: str, formula_id: FormulaId) -> tuple[int, int]:
    """Return the frozen Stage 3B-v1 full/min window for one feature archetype."""
    if frequency == "weekly":
        return 260, 156
    if frequency == "quarterly":
        return 40, 20
    if frequency == "monthly":
        if formula_id in {FormulaId.LEVEL, FormulaId.MOVING_AVERAGE_K}:
            return 60, 36
        return 120, 60
    raise ValueError(f"Unsupported signal frequency: {frequency}")


def load_signal_catalog(
    path: Path = DEFAULT_SIGNAL_CONFIG_PATH,
    *,
    source_registry: SourceRegistryCatalog | None = None,
    feature_catalog: FeatureCatalog | None = None,
) -> SignalCatalog:
    """Load, bind, validate, and hash all 123 one-to-one signal definitions."""
    registry = source_registry or load_source_registry()
    features = feature_catalog or load_feature_catalog(source_registry=registry)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("signals"), list):
        raise ValueError("Signal configuration requires a signals list")
    if raw.get("normalization_spec_version") != "stage3b-v1":
        raise ValueError("NORMALIZATION_SPEC_VERSION_MISMATCH")
    if raw.get("normalizer_registry_version") != NORMALIZER_REGISTRY_VERSION:
        raise ValueError("NORMALIZER_REGISTRY_VERSION_MISMATCH")
    if raw.get("required_feature_spec_version") != features.feature_spec_version:
        raise ValueError("FEATURE_SPEC_MISMATCH")
    if raw.get("required_source_registry_version") != registry.registry_policy_version:
        raise ValueError("REGISTRY_VERSION_MISMATCH")
    if raw.get("required_source_registry_hash") != registry.registry_hash:
        raise ValueError("REGISTRY_HASH_MISMATCH")
    if raw.get("distribution_includes_current") is not False:
        raise ValueError("Stage 3B-v1 distribution must exclude the current observation")

    raw_signals: list[Any] = raw["signals"]
    signals = [SignalDefinition.model_validate(item) for item in raw_signals]
    feature_by_id = features.by_feature()
    configured_feature_ids = [item.feature_id for item in signals]
    signal_ids = [item.signal_id for item in signals]
    if len(signals) != EXPECTED_SIGNAL_COUNT:
        raise ValueError(f"Signal inventory must contain {EXPECTED_SIGNAL_COUNT} definitions")
    if len(configured_feature_ids) != len(set(configured_feature_ids)):
        raise ValueError("One feature cannot map to more than one Stage 3B-v1 signal")
    if len(signal_ids) != len(set(signal_ids)):
        raise ValueError("Signal IDs must be globally unique")
    if set(configured_feature_ids) != set(feature_by_id):
        missing = sorted(set(feature_by_id) - set(configured_feature_ids))
        extra = sorted(set(configured_feature_ids) - set(feature_by_id))
        raise ValueError(f"SIGNAL_INVENTORY_DRIFT:missing={missing}:extra={extra}")

    registry_by_id = registry.by_indicator()
    for signal in signals:
        indicator, feature = feature_by_id[signal.feature_id]
        if signal.indicator_id != indicator.indicator_id:
            raise ValueError(f"{signal.signal_id} indicator ID drift")
        if signal.feature_role != feature.role:
            raise ValueError(f"{signal.signal_id} feature role drift")
        expected_id = f"{signal.feature_id}__{signal.economic_axis.value}__SIGNAL"
        if signal.signal_id != expected_id:
            raise ValueError(f"Unstable signal ID: {signal.signal_id} != {expected_id}")
        if not signal.redundancy_group.strip():
            raise ValueError(f"{signal.signal_id} requires redundancy metadata")
        frequency = registry_by_id[indicator.indicator_id].canonical.frequency
        expected_window, expected_min = expected_window_policy(frequency, feature.formula_id)
        if (signal.full_window, signal.min_history) != (expected_window, expected_min):
            raise ValueError(
                f"{signal.signal_id} window policy must be {expected_window}/{expected_min}"
            )

    primary_count = sum(item.feature_role == "primary" for item in signals)
    supporting_count = sum(item.feature_role == "supporting" for item in signals)
    if primary_count != EXPECTED_PRIMARY_COUNT or supporting_count != EXPECTED_SUPPORTING_COUNT:
        raise ValueError("Signal role inventory must remain 33 primary and 90 supporting")

    hash_payload = {
        "schema_version": str(raw["schema_version"]),
        "normalization_spec_version": str(raw["normalization_spec_version"]),
        "normalizer_registry_version": str(raw["normalizer_registry_version"]),
        "required_feature_spec_version": str(raw["required_feature_spec_version"]),
        "required_source_registry_version": str(raw["required_source_registry_version"]),
        "required_source_registry_hash": str(raw["required_source_registry_hash"]),
        "distribution_includes_current": bool(raw["distribution_includes_current"]),
        "extreme_value_threshold": str(raw["extreme_value_threshold"]),
        "low_dispersion_relative_threshold": str(raw["low_dispersion_relative_threshold"]),
        "signals": [item.model_dump(mode="json") for item in signals],
    }
    specification_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return SignalCatalog(
        **{key: value for key, value in hash_payload.items() if key != "signals"},
        signals=signals,
        source_path=path,
        specification_hash=specification_hash,
    )
