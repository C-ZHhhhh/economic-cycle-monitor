"""Load and validate the unique Stage 3C pillar specification authority."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from app.features.config import load_feature_catalog
from app.features.schemas import FeatureCatalog
from app.pillars.schemas import (
    AggregationMethod,
    ContributionMapping,
    CoveragePolicy,
    PillarCatalog,
    PillarDefinition,
    PillarRole,
    SupportingSignalDefinition,
    SupportingUsage,
)
from app.signals.config import load_signal_catalog
from app.signals.schemas import Direction, SignalCatalog
from app.source_registry.config import load_source_registry
from app.source_registry.schemas import SourceRegistryCatalog

DEFAULT_PILLAR_CONFIG_PATH = Path("config/pillars/us_macro_pillars.yaml")
EXPECTED_PILLAR_COUNT = 9
EXPECTED_CORE_COUNT = 5
EXPECTED_CONTEXTUAL_COUNT = 4
EXPECTED_SIGNAL_COUNT = 123
EXPECTED_PRIMARY_COUNT = 33
EXPECTED_SUPPORTING_COUNT = 90
EXPECTED_PILLAR_ROLES = {
    "US_MACRO__REAL_ACTIVITY_AND_DEMAND": PillarRole.CORE,
    "US_MACRO__LABOR_STRENGTH": PillarRole.CORE,
    "US_MACRO__INFLATION_PRESSURE": PillarRole.CORE,
    "US_MACRO__MANUFACTURING_AND_INVESTMENT": PillarRole.CORE,
    "US_MACRO__HOUSING_STRENGTH": PillarRole.CORE,
    "US_MACRO__HOUSEHOLD_FINANCIAL_CUSHION": PillarRole.CONTEXTUAL,
    "US_MACRO__CREDIT_USAGE_PRESSURE": PillarRole.CONTEXTUAL,
    "US_MACRO__INVENTORY_ACCUMULATION_PRESSURE": PillarRole.CONTEXTUAL,
    "US_MACRO__INVENTORY_ABSORPTION_STRENGTH": PillarRole.CONTEXTUAL,
}


def _hash_payload(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _required_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Pillar configuration requires a {key} mapping")
    return value


def load_pillar_catalog(
    path: Path = DEFAULT_PILLAR_CONFIG_PATH,
    *,
    source_registry: SourceRegistryCatalog | None = None,
    feature_catalog: FeatureCatalog | None = None,
    signal_catalog: SignalCatalog | None = None,
) -> PillarCatalog:
    """Load, expand, validate, and hash all Stage 3C-v1 pillar bindings."""
    registry = source_registry or load_source_registry()
    features = feature_catalog or load_feature_catalog(source_registry=registry)
    signals = signal_catalog or load_signal_catalog(
        source_registry=registry, feature_catalog=features
    )
    raw_value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw_value, dict) or not isinstance(raw_value.get("pillars"), list):
        raise ValueError("Pillar configuration requires a pillars list")
    raw: dict[str, Any] = raw_value

    if raw.get("pillar_spec_version") != "stage3c-v1":
        raise ValueError("PILLAR_SPEC_VERSION_MISMATCH")
    if raw.get("required_source_registry_version") != registry.registry_policy_version:
        raise ValueError("REGISTRY_VERSION_MISMATCH")
    if raw.get("required_source_registry_hash") != registry.registry_hash:
        raise ValueError("REGISTRY_HASH_MISMATCH")
    if raw.get("required_feature_spec_version") != features.feature_spec_version:
        raise ValueError("FEATURE_SPEC_MISMATCH")
    if raw.get("required_normalization_spec_version") != signals.normalization_spec_version:
        raise ValueError("NORMALIZATION_SPEC_VERSION_MISMATCH")
    if raw.get("required_normalization_spec_hash") != signals.specification_hash:
        raise ValueError("NORMALIZATION_SPEC_HASH_MISMATCH")

    expected = (
        int(raw.get("expected_signal_count", -1)),
        int(raw.get("expected_primary_count", -1)),
        int(raw.get("expected_supporting_count", -1)),
    )
    actual = (
        len(signals.signals),
        sum(item.feature_role == "primary" for item in signals.signals),
        sum(item.feature_role == "supporting" for item in signals.signals),
    )
    if expected != (EXPECTED_SIGNAL_COUNT, EXPECTED_PRIMARY_COUNT, EXPECTED_SUPPORTING_COUNT):
        raise ValueError("PILLAR_EXPECTED_SIGNAL_INVENTORY_DRIFT")
    if actual != expected:
        raise ValueError(f"SIGNAL_INVENTORY_DRIFT:expected={expected}:actual={actual}")

    if raw.get("aggregation_method") != AggregationMethod.DIRECT_EQUAL_INDICATOR.value:
        raise ValueError("UNSUPPORTED_AGGREGATION")
    if raw.get("contribution_mapping") != ContributionMapping.TANH_SATURATION.value:
        raise ValueError("UNSUPPORTED_CONTRIBUTION_MAPPING")
    aggregation = AggregationMethod.DIRECT_EQUAL_INDICATOR
    contribution = ContributionMapping.TANH_SATURATION
    parameters = _required_mapping(raw, "contribution_mapping_parameters")
    coverage = CoveragePolicy.model_validate(_required_mapping(raw, "coverage_policy"))
    if Decimal(str(parameters.get("amplitude"))) != Decimal(1) or Decimal(
        str(parameters.get("scale"))
    ) != Decimal(2):
        raise ValueError("TANH_PARAMETER_DRIFT")
    if (coverage.minimum_numerator, coverage.minimum_denominator) != (2, 3):
        raise ValueError("COVERAGE_POLICY_DRIFT")
    if str(raw.get("confidence_method")) != "COVERAGE_ONLY":
        raise ValueError("UNSUPPORTED_CONFIDENCE_METHOD")
    if str(raw.get("latest_selection_method")) != "LATEST_KNOWN_SAME_EOD":
        raise ValueError("UNSUPPORTED_LATEST_SELECTION_METHOD")

    default_usage = SupportingUsage(str(raw.get("supporting_usage_default")))
    override_raw = _required_mapping(raw, "supporting_usage_overrides")
    overrides = {key: SupportingUsage(str(value)) for key, value in override_raw.items()}
    signal_by_id = signals.by_signal()
    unknown_overrides = sorted(set(overrides) - set(signal_by_id))
    if unknown_overrides:
        raise ValueError(f"UNKNOWN_SUPPORTING_SIGNALS:{unknown_overrides}")

    primary_by_indicator: dict[str, list[Any]] = {}
    supporting_by_indicator: dict[str, list[Any]] = {}
    for item in signals.signals:
        target = primary_by_indicator if item.feature_role == "primary" else supporting_by_indicator
        target.setdefault(item.indicator_id, []).append(item)
    if any(len(items) != 1 for items in primary_by_indicator.values()):
        raise ValueError("Every indicator must have exactly one primary signal")

    expanded: list[PillarDefinition] = []
    configured_indicators: list[str] = []
    for pillar_raw_value in raw["pillars"]:
        if not isinstance(pillar_raw_value, dict):
            raise ValueError("Each pillar must be a mapping")
        pillar_raw: dict[str, Any] = pillar_raw_value
        indicator_ids = pillar_raw.pop("primary_indicator_ids", None)
        if not isinstance(indicator_ids, list) or not all(
            isinstance(item, str) for item in indicator_ids
        ):
            raise ValueError("Each pillar requires primary_indicator_ids")
        configured_indicators.extend(indicator_ids)
        primary_ids: list[str] = []
        supporting_bindings: list[SupportingSignalDefinition] = []
        for indicator_id in indicator_ids:
            if indicator_id not in primary_by_indicator:
                raise ValueError(f"UNKNOWN_PRIMARY_INDICATOR:{indicator_id}")
            primary_ids.append(primary_by_indicator[indicator_id][0].signal_id)
            for item in supporting_by_indicator.get(indicator_id, []):
                usage = overrides.get(item.signal_id, default_usage)
                if (
                    item.direction is Direction.NEUTRAL_CONTEXT_ONLY
                    and usage is not SupportingUsage.NOT_USED_V1
                ):
                    raise ValueError("Context-only signal cannot cast a supporting vote")
                if (
                    item.direction is not Direction.NEUTRAL_CONTEXT_ONLY
                    and usage is SupportingUsage.NOT_USED_V1
                ):
                    raise ValueError(f"Directional signal unexpectedly disabled:{item.signal_id}")
                supporting_bindings.append(
                    SupportingSignalDefinition(
                        signal_id=item.signal_id,
                        related_pillar_id=str(pillar_raw["pillar_id"]),
                        redundancy_group=item.redundancy_group,
                        intended_usage=usage,
                    )
                )
        expanded.append(
            PillarDefinition.model_validate(
                {
                    **pillar_raw,
                    "primary_signal_ids": primary_ids,
                    "supporting_signals": supporting_bindings,
                }
            )
        )

    if len(expanded) != EXPECTED_PILLAR_COUNT:
        raise ValueError("Pillar taxonomy must contain exactly nine pillars")
    pillar_ids = [item.pillar_id for item in expanded]
    if len(pillar_ids) != len(set(pillar_ids)):
        raise ValueError("Pillar IDs must be unique")
    if {item.pillar_id: item.pillar_role for item in expanded} != EXPECTED_PILLAR_ROLES:
        raise ValueError("PILLAR_TAXONOMY_DRIFT")
    core_count = sum(item.pillar_role is PillarRole.CORE for item in expanded)
    contextual_count = sum(item.pillar_role is PillarRole.CONTEXTUAL for item in expanded)
    if (core_count, contextual_count) != (EXPECTED_CORE_COUNT, EXPECTED_CONTEXTUAL_COUNT):
        raise ValueError("Pillar roles must remain five core and four contextual")
    if len(configured_indicators) != len(set(configured_indicators)):
        raise ValueError("A primary indicator cannot map to more than one pillar")
    if set(configured_indicators) != set(primary_by_indicator):
        missing = sorted(set(primary_by_indicator) - set(configured_indicators))
        extra = sorted(set(configured_indicators) - set(primary_by_indicator))
        raise ValueError(f"PRIMARY_MAPPING_DRIFT:missing={missing}:extra={extra}")
    configured_primary = [signal for item in expanded for signal in item.primary_signal_ids]
    configured_supporting = [
        signal.signal_id for item in expanded for signal in item.supporting_signals
    ]
    expected_primary = {
        item.signal_id for item in signals.signals if item.feature_role == "primary"
    }
    expected_supporting = {
        item.signal_id for item in signals.signals if item.feature_role == "supporting"
    }
    if (
        len(configured_primary) != EXPECTED_PRIMARY_COUNT
        or set(configured_primary) != expected_primary
    ):
        raise ValueError("PRIMARY_SIGNAL_MAPPING_INCOMPLETE")
    if (
        len(configured_supporting) != EXPECTED_SUPPORTING_COUNT
        or set(configured_supporting) != expected_supporting
    ):
        raise ValueError("SUPPORTING_SIGNAL_MAPPING_INCOMPLETE")

    hash_payload = {
        "schema_version": str(raw["schema_version"]),
        "pillar_spec_version": str(raw["pillar_spec_version"]),
        "required_source_registry_version": str(raw["required_source_registry_version"]),
        "required_source_registry_hash": str(raw["required_source_registry_hash"]),
        "required_feature_spec_version": str(raw["required_feature_spec_version"]),
        "required_normalization_spec_version": str(raw["required_normalization_spec_version"]),
        "required_normalization_spec_hash": str(raw["required_normalization_spec_hash"]),
        "expected_signal_count": expected[0],
        "expected_primary_count": expected[1],
        "expected_supporting_count": expected[2],
        "aggregation_method": aggregation.value,
        "contribution_mapping": contribution.value,
        "contribution_mapping_parameters": parameters,
        "coverage_policy": coverage.model_dump(mode="json"),
        "confidence_method": str(raw["confidence_method"]),
        "latest_selection_method": str(raw["latest_selection_method"]),
        "latest_known_search_months": int(raw["latest_known_search_months"]),
        "pillars": [item.model_dump(mode="json") for item in expanded],
    }
    return PillarCatalog(
        **hash_payload,
        source_path=path,
        specification_hash=_hash_payload(hash_payload),
    )
