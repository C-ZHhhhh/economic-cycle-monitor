"""Load and validate the unique Stage 3E macro-regime authority."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from app.composites.config import load_composite_catalog
from app.composites.schemas import CompositeCatalog
from app.regimes.schemas import (
    BaseTaxonomy,
    ContextPolicy,
    EvaluationCadence,
    GrowthCondition,
    HysteresisPolicy,
    InflationCondition,
    MarginMethod,
    MissingAxisPolicy,
    PartialStatePolicy,
    RegimeArchitecture,
    RegimeCatalog,
    TrajectoryMethod,
)

DEFAULT_REGIME_CONFIG_PATH = Path("config/regimes/us_macro_regime.yaml")
EXPECTED_REGIME_IDS = [
    f"US_MACRO_REGIME__GROWTH_{growth.value}__INFLATION_PRESSURE_{inflation.value.removesuffix('_PRESSURE')}"
    for growth in GrowthCondition
    for inflation in InflationCondition
]


def _hash_payload(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_regime_catalog(
    path: Path = DEFAULT_REGIME_CONFIG_PATH,
    *,
    composite_catalog: CompositeCatalog | None = None,
) -> RegimeCatalog:
    """Load, bind, validate, and hash the Stage 3E-v1 specification."""
    composite = composite_catalog or load_composite_catalog()
    raw_value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw_value, dict):
        raise ValueError("Regime configuration must be a mapping")
    raw: dict[str, Any] = raw_value
    if raw.get("regime_spec_version") != "stage3e-v1":
        raise ValueError("REGIME_SPEC_VERSION_MISMATCH")

    bindings = {
        "required_stage3d_spec_version": composite.composite_spec_version,
        "required_stage3d_spec_hash": composite.specification_hash,
        "required_source_registry_version": composite.required_source_registry_version,
        "required_source_registry_hash": composite.required_source_registry_hash,
        "required_feature_spec_version": composite.required_feature_spec_version,
        "required_normalization_spec_version": composite.required_normalization_spec_version,
        "required_normalization_spec_hash": composite.required_normalization_spec_hash,
        "required_pillar_spec_version": composite.required_pillar_spec_version,
        "required_pillar_spec_hash": composite.required_pillar_spec_hash,
    }
    for key, expected in bindings.items():
        if raw.get(key) != expected:
            raise ValueError(f"REGIME_BINDING_MISMATCH:{key}")

    catalog = RegimeCatalog.model_validate(
        {**raw, "source_path": path, "specification_hash": "pending"}
    )
    if catalog.architecture is not RegimeArchitecture.LEVEL_PLUS_TRAJECTORY:
        raise ValueError("UNSUPPORTED_REGIME_ARCHITECTURE")
    if catalog.base_taxonomy is not BaseTaxonomy.HIERARCHICAL_3X3:
        raise ValueError("UNSUPPORTED_BASE_TAXONOMY")
    if catalog.evaluation_cadence is not EvaluationCadence.ANY_EOD_STATELESS:
        raise ValueError("UNSUPPORTED_EVALUATION_CADENCE")
    if catalog.hysteresis_policy is not HysteresisPolicy.NONE:
        raise ValueError("UNSUPPORTED_HYSTERESIS_POLICY")
    if catalog.partial_state_policy is not PartialStatePolicy.ALLOW_IF_AXES_ELIGIBLE:
        raise ValueError("UNSUPPORTED_PARTIAL_STATE_POLICY")
    if catalog.missing_axis_policy is not MissingAxisPolicy.FAIL_CLOSED:
        raise ValueError("UNSUPPORTED_MISSING_AXIS_POLICY")
    if catalog.context_policy is not ContextPolicy.PASS_THROUGH_ONLY:
        raise ValueError("UNSUPPORTED_CONTEXT_POLICY")
    if catalog.trajectory.method is not TrajectoryMethod.DELTA_3M_REAL_TIME_PATH:
        raise ValueError("UNSUPPORTED_TRAJECTORY_METHOD")
    if catalog.trajectory.horizon_months != 3:
        raise ValueError("TRAJECTORY_HORIZON_DRIFT")
    if catalog.classification_margin.method is not MarginMethod.DISTANCE_TO_NEAREST_BOUNDARY:
        raise ValueError("UNSUPPORTED_MARGIN_METHOD")

    for axis in (catalog.growth, catalog.inflation):
        if axis.lower_threshold != Decimal("-0.10") or axis.upper_threshold != Decimal("0.10"):
            raise ValueError("LEVEL_THRESHOLD_DRIFT")
        if axis.minimum_confidence.numerator != 2 or axis.minimum_confidence.denominator != 3:
            raise ValueError("CONFIDENCE_GATE_DRIFT")
    if catalog.trajectory.lower_threshold != Decimal(
        "-0.10"
    ) or catalog.trajectory.upper_threshold != Decimal("0.10"):
        raise ValueError("TRAJECTORY_THRESHOLD_DRIFT")
    if catalog.classification_margin.warning_threshold != Decimal("0.05"):
        raise ValueError("MARGIN_WARNING_THRESHOLD_DRIFT")

    keys = [(item.growth_condition, item.inflation_condition) for item in catalog.base_regimes]
    expected_keys = {
        (growth, inflation) for growth in GrowthCondition for inflation in InflationCondition
    }
    if len(keys) != 9 or len(set(keys)) != 9 or set(keys) != expected_keys:
        raise ValueError("BASE_REGIME_TAXONOMY_INCOMPLETE")
    if [item.regime_id for item in catalog.base_regimes] != EXPECTED_REGIME_IDS:
        raise ValueError("BASE_REGIME_ID_DRIFT")
    if len({item.display_name for item in catalog.base_regimes}) != 9:
        raise ValueError("BASE_REGIME_DISPLAY_NAMES_MUST_BE_UNIQUE")

    return RegimeCatalog.model_validate(
        {**raw, "source_path": path, "specification_hash": _hash_payload(raw)}
    )
