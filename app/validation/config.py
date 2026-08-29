"""Load and fail-closed validate the Stage 3F validation authority."""

from pathlib import Path
from typing import Any

import yaml

from app.composites.config import load_composite_catalog
from app.features.config import load_feature_catalog
from app.pillars.config import load_pillar_catalog
from app.regimes.config import load_regime_catalog
from app.signals.config import load_signal_catalog
from app.source_registry.config import load_source_registry
from app.validation.hashing import semantic_hash
from app.validation.schemas import (
    AuthorityBindings,
    UniverseMode,
    ValidationCatalog,
    ValidationPhase,
)

DEFAULT_VALIDATION_CONFIG_PATH = Path("config/validation/us_macro_stage3f.yaml")


def _active_authorities() -> AuthorityBindings:
    registry = load_source_registry()
    features = load_feature_catalog(source_registry=registry)
    signals = load_signal_catalog(source_registry=registry, feature_catalog=features)
    pillars = load_pillar_catalog(source_registry=registry, signal_catalog=signals)
    composite = load_composite_catalog(pillar_catalog=pillars)
    regime = load_regime_catalog(composite_catalog=composite)
    return AuthorityBindings(
        source_registry_version=registry.registry_policy_version,
        source_registry_hash=registry.registry_hash,
        feature_spec_version=features.feature_spec_version,
        feature_spec_hash=features.specification_hash,
        normalization_spec_version=signals.normalization_spec_version,
        normalization_spec_hash=signals.specification_hash,
        pillar_spec_version=pillars.pillar_spec_version,
        pillar_spec_hash=pillars.specification_hash,
        composite_spec_version=composite.composite_spec_version,
        composite_spec_hash=composite.specification_hash,
        regime_spec_version=regime.regime_spec_version,
        regime_spec_hash=regime.specification_hash,
    )


def _configured_authorities(raw: dict[str, Any]) -> AuthorityBindings:
    return AuthorityBindings(
        source_registry_version=str(raw.pop("required_source_registry_version")),
        source_registry_hash=str(raw.pop("required_source_registry_hash")),
        feature_spec_version=str(raw.pop("required_feature_spec_version")),
        feature_spec_hash=str(raw.pop("required_feature_spec_hash")),
        normalization_spec_version=str(raw.pop("required_normalization_spec_version")),
        normalization_spec_hash=str(raw.pop("required_normalization_spec_hash")),
        pillar_spec_version=str(raw.pop("required_pillar_spec_version")),
        pillar_spec_hash=str(raw.pop("required_pillar_spec_hash")),
        composite_spec_version=str(raw.pop("required_composite_spec_version")),
        composite_spec_hash=str(raw.pop("required_composite_spec_hash")),
        regime_spec_version=str(raw.pop("required_regime_spec_version")),
        regime_spec_hash=str(raw.pop("required_regime_spec_hash")),
    )


def load_validation_catalog(
    path: Path = DEFAULT_VALIDATION_CONFIG_PATH,
    *,
    active_authorities: AuthorityBindings | None = None,
) -> ValidationCatalog:
    """Load Stage 3F without modifying or shadowing any production authority."""
    raw_value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw_value, dict):
        raise ValueError("Validation configuration must be a mapping")
    raw: dict[str, Any] = dict(raw_value)
    if raw.get("validation_spec_version") != "stage3f-v1":
        raise ValueError("VALIDATION_SPEC_VERSION_MISMATCH")
    configured = _configured_authorities(raw)
    active = active_authorities or _active_authorities()
    for field_name in AuthorityBindings.model_fields:
        if getattr(configured, field_name) != getattr(active, field_name):
            raise ValueError(f"PRODUCTION_AUTHORITY_DRIFT:{field_name}")

    hash_payload = dict(raw_value)
    catalog = ValidationCatalog.model_validate(
        {
            **raw,
            "authorities": configured,
            "source_path": path,
            "specification_hash": semantic_hash(hash_payload),
        }
    )
    if catalog.validation_phase is not ValidationPhase.PILOT:
        raise ValueError("FULL_HISTORICAL_RUN_NOT_APPROVED")
    if catalog.universe_modes != [
        UniverseMode.FULL_FIXED_UNIVERSE,
        UniverseMode.DYNAMIC_REAL_TIME_UNIVERSE,
    ]:
        raise ValueError("UNIVERSE_MODE_DRIFT")
    if catalog.cadence.primary.value != "RELEASE_EVENT_EOD":
        raise ValueError("PRIMARY_CADENCE_DRIFT")
    if catalog.cadence.benchmark.value != "MONTH_END_EOD":
        raise ValueError("BENCHMARK_CADENCE_DRIFT")
    if catalog.cadence.intraday_reconstruction:
        raise ValueError("INTRADAY_RECONSTRUCTION_PROHIBITED")
    if catalog.budgets.pilot.external_alfred_http_requests != 2000:
        raise ValueError("PILOT_REQUEST_CAP_DRIFT")
    if catalog.budgets.provisional_full_run.approved:
        raise ValueError("FULL_HISTORICAL_RUN_NOT_APPROVED")
    if catalog.artifact_formats.parquet_enabled:
        raise ValueError("PARQUET_NOT_APPROVED")
    if catalog.materialization.sensitivity_refetch_allowed:
        raise ValueError("SENSITIVITY_REFETCH_PROHIBITED")
    if catalog.hysteresis_research.production_writeback:
        raise ValueError("HYSTERESIS_PRODUCTION_WRITEBACK_PROHIBITED")
    if catalog.revision_sensitivity.build_alternate_latest_revised_history:
        raise ValueError("ALTERNATE_REVISED_HISTORY_NOT_APPROVED")
    return catalog
