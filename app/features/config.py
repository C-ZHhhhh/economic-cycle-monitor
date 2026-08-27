"""Load and validate the unique Stage 3A feature specification authority."""

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.features.formulas import FORMULA_REGISTRY, FORMULA_REGISTRY_VERSION
from app.features.schemas import (
    FeatureCatalog,
    FeatureDefinition,
    FormulaId,
    IndicatorFeatureSpec,
)
from app.source_registry.config import load_source_registry
from app.source_registry.schemas import SourceRegistryCatalog

DEFAULT_FEATURE_CONFIG_PATH = Path("config/features/us_macro_features.yaml")
POINT_UNITS = {"percent": "percentage_points", "rate": "rate_points", "ratio": "ratio_points"}
COMPOUND_FORBIDDEN_UNITS = frozenset(POINT_UNITS)


def _expected_output_unit(formula_id: FormulaId, canonical_unit: str) -> str:
    if formula_id in {
        FormulaId.LEVEL,
        FormulaId.DIFF_K,
        FormulaId.AVG_PERIOD_DIFF_K,
        FormulaId.MOVING_AVERAGE_K,
        FormulaId.MA4W_CHANGE_KW,
    }:
        return canonical_unit
    if formula_id is FormulaId.POINT_DIFF_K:
        try:
            return POINT_UNITS[canonical_unit]
        except KeyError as error:
            raise ValueError(f"POINT_DIFF_K does not support unit {canonical_unit}") from error
    if formula_id in {
        FormulaId.MONTHLY_ANNUALIZED_K,
        FormulaId.QUARTERLY_QOQ_ANNUALIZED,
        FormulaId.MA3_BLOCK_GROWTH_ANNUALIZED,
    }:
        return "percent_annualized"
    if formula_id in {
        FormulaId.PCT_CHANGE_K,
        FormulaId.MONTHLY_YOY,
        FormulaId.QUARTERLY_YOY,
        FormulaId.MA4W_YOY_PCT,
    }:
        return "percent"
    if formula_id in {
        FormulaId.GROWTH_ACCEL_QOQ_1Q,
        FormulaId.GROWTH_ACCEL_3M_NONOVERLAP,
    }:
        return "annualized_percentage_points"
    raise ValueError(f"Unsupported formula {formula_id}")


def _validate_window(feature: FeatureDefinition) -> None:
    fixed = {
        FormulaId.LEVEL: 1,
        FormulaId.MONTHLY_YOY: 12,
        FormulaId.QUARTERLY_QOQ_ANNUALIZED: 1,
        FormulaId.QUARTERLY_YOY: 4,
        FormulaId.MA3_BLOCK_GROWTH_ANNUALIZED: 3,
        FormulaId.GROWTH_ACCEL_QOQ_1Q: 1,
        FormulaId.GROWTH_ACCEL_3M_NONOVERLAP: 3,
        FormulaId.MA4W_YOY_PCT: 52,
    }
    expected = fixed.get(feature.formula_id)
    if expected is not None and feature.window != expected:
        raise ValueError(f"{feature.feature_id} requires window={expected}")
    if feature.formula_id is FormulaId.MONTHLY_ANNUALIZED_K and 12 % feature.window:
        raise ValueError(f"{feature.feature_id} monthly annualization window must divide 12")


def _validate_feature(
    feature: FeatureDefinition,
    *,
    indicator_id: str,
    frequency: str,
    canonical_unit: str,
) -> None:
    if not feature.feature_id.startswith(f"{indicator_id}__"):
        raise ValueError(f"Feature ID must be namespaced by {indicator_id}: {feature.feature_id}")
    contract = FORMULA_REGISTRY[feature.formula_id]
    if frequency not in contract.frequencies:
        raise ValueError(f"{feature.feature_id} formula is incompatible with {frequency}")
    if (
        feature.formula_id
        in {
            FormulaId.PCT_CHANGE_K,
            FormulaId.MONTHLY_ANNUALIZED_K,
            FormulaId.MONTHLY_YOY,
            FormulaId.QUARTERLY_QOQ_ANNUALIZED,
            FormulaId.QUARTERLY_YOY,
            FormulaId.MA3_BLOCK_GROWTH_ANNUALIZED,
            FormulaId.GROWTH_ACCEL_QOQ_1Q,
            FormulaId.GROWTH_ACCEL_3M_NONOVERLAP,
        }
        and canonical_unit in COMPOUND_FORBIDDEN_UNITS
    ):
        raise ValueError(f"Compound growth is forbidden for {canonical_unit}: {feature.feature_id}")
    expected_unit = _expected_output_unit(feature.formula_id, canonical_unit)
    if feature.output_unit != expected_unit:
        raise ValueError(
            f"{feature.feature_id} output unit {feature.output_unit} != {expected_unit}"
        )
    _validate_window(feature)


def load_feature_catalog(
    path: Path = DEFAULT_FEATURE_CONFIG_PATH,
    *,
    source_registry: SourceRegistryCatalog | None = None,
) -> FeatureCatalog:
    """Load, bind, and deterministically hash the complete Stage 3A specification."""
    registry = source_registry or load_source_registry()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("indicators"), list):
        raise ValueError("Feature configuration requires an indicators list")
    if raw.get("required_source_registry_version") != registry.registry_policy_version:
        raise ValueError("REGISTRY_VERSION_MISMATCH")
    if raw.get("required_source_registry_hash") != registry.registry_hash:
        raise ValueError("REGISTRY_HASH_MISMATCH")
    if raw.get("formula_registry_version") != FORMULA_REGISTRY_VERSION:
        raise ValueError("FORMULA_REGISTRY_VERSION_MISMATCH")

    raw_indicators: list[Any] = raw["indicators"]
    indicators = [IndicatorFeatureSpec.model_validate(item) for item in raw_indicators]
    registry_by_id = registry.by_indicator()
    configured_ids = [item.indicator_id for item in indicators]
    if len(configured_ids) != len(set(configured_ids)):
        raise ValueError("Feature indicator IDs must be unique")
    if set(configured_ids) != set(registry_by_id):
        missing = sorted(set(registry_by_id) - set(configured_ids))
        extra = sorted(set(configured_ids) - set(registry_by_id))
        raise ValueError(f"FEATURE_INVENTORY_DRIFT:missing={missing}:extra={extra}")
    feature_ids = [feature.feature_id for item in indicators for feature in item.features]
    if len(feature_ids) != len(set(feature_ids)):
        raise ValueError("Feature IDs must be globally unique")
    for indicator in indicators:
        policy = registry_by_id[indicator.indicator_id]
        for feature in indicator.features:
            _validate_feature(
                feature,
                indicator_id=indicator.indicator_id,
                frequency=policy.canonical.frequency,
                canonical_unit=policy.canonical.unit,
            )
    hash_payload = {
        "schema_version": str(raw["schema_version"]),
        "feature_spec_version": str(raw["feature_spec_version"]),
        "formula_registry_version": str(raw["formula_registry_version"]),
        "required_source_registry_version": str(raw["required_source_registry_version"]),
        "required_source_registry_hash": str(raw["required_source_registry_hash"]),
        "indicators": [item.model_dump(mode="json") for item in indicators],
    }
    specification_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FeatureCatalog(
        **{key: hash_payload[key] for key in hash_payload if key != "indicators"},
        indicators=indicators,
        source_path=path,
        specification_hash=specification_hash,
    )
