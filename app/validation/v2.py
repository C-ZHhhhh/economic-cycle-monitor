"""Stage 3F-v2 authority for the bounded DLCR-003 offline equivalence gate."""

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.source_registry.materialization import load_pit_materialization_catalog
from app.validation.config import _active_authorities
from app.validation.schemas import AuthorityBindings

DEFAULT_STAGE3F_V2_PATH = Path("config/validation/us_macro_stage3f_v2.yaml")


class EquivalenceAuthority(BaseModel):
    """Hard gate comparing every First Pilot exact snapshot without network access."""

    model_config = ConfigDict(extra="forbid")

    baseline: Literal["BASELINE_EXACT_PIT"]
    candidate: Literal["SEMANTIC_INFORMATION_SET_RESOLUTION"]
    required_snapshot_pairs: Literal[594]
    external_alfred_http_requests: Literal[0]
    require_economic_semantic_equivalence: Literal[True]
    require_evaluation_hash_equivalence: Literal[True]


class Stage3FV2Catalog(BaseModel):
    """Independent v2 provenance authority that leaves Stage 3F-v1 immutable."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    validation_spec_version: Literal["stage3f-v2"]
    validation_phase: Literal["PILOT"]
    purpose: Literal["DLCR003_OFFLINE_EQUIVALENCE"]
    required_source_registry_version: str
    required_source_registry_hash: str
    required_feature_spec_version: str
    required_feature_spec_hash: str
    required_normalization_spec_version: str
    required_normalization_spec_hash: str
    required_pillar_spec_version: str
    required_pillar_spec_hash: str
    required_composite_spec_version: str
    required_composite_spec_hash: str
    required_regime_spec_version: str
    required_regime_spec_hash: str
    required_pit_materialization_spec_version: str
    required_pit_materialization_spec_hash: str
    required_calculation_provenance_version: str
    required_calculation_provenance_hash: str
    equivalence: EquivalenceAuthority
    bulk_observations_enabled: Literal[False]
    second_pilot_approved: Literal[False]
    full_historical_run_approved: Literal[False]
    source_path: Path
    specification_hash: str = Field(min_length=64, max_length=64)


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_stage3f_v2_catalog(
    path: Path = DEFAULT_STAGE3F_V2_PATH,
) -> Stage3FV2Catalog:
    """Load v2 and bind it to all unchanged economic and new PIT authorities."""
    raw_value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw_value, dict):
        raise ValueError("Stage3F-v2 configuration must be a mapping")
    raw = dict(raw_value)
    economic = _active_authorities()
    for field_name in AuthorityBindings.model_fields:
        configured = raw[f"required_{field_name}"]
        if configured != getattr(economic, field_name):
            raise ValueError(f"PRODUCTION_AUTHORITY_DRIFT:{field_name}")
    materialization = load_pit_materialization_catalog()
    expected_pit = {
        "required_pit_materialization_spec_version": (
            materialization.pit_materialization_spec_version
        ),
        "required_pit_materialization_spec_hash": materialization.specification_hash,
        "required_calculation_provenance_version": (materialization.calculation_provenance.version),
        "required_calculation_provenance_hash": (materialization.calculation_provenance_hash),
    }
    for key, expected in expected_pit.items():
        configured = raw[key]
        if configured == "__COMPUTED_AT_LOAD__":
            raw[key] = expected
        elif configured != expected:
            raise ValueError(f"PIT_AUTHORITY_DRIFT:{key}")
    return Stage3FV2Catalog.model_validate(
        {**raw, "source_path": path, "specification_hash": _hash(raw_value)}
    )
