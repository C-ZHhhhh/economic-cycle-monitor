"""Load the hash-bound US macro explanation authority and prompt."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.composites.config import load_composite_catalog
from app.features.config import load_feature_catalog
from app.pillars.config import load_pillar_catalog
from app.regimes.config import load_regime_catalog
from app.signals.config import load_signal_catalog
from app.source_registry.config import load_source_registry
from app.source_registry.current_freshness import load_current_freshness_catalog
from app.source_registry.materialization import load_pit_materialization_catalog
from app.source_registry.revision_provenance import load_revision_provenance_catalog

DEFAULT_EXPLANATION_CONFIG_PATH = Path("config/agents/us_macro_explanation.yaml")
DEFAULT_EXPLANATION_PROMPT_PATH = Path("config/prompts/us_macro_explanation_v1.md")


def hash_payload(value: object) -> str:
    """Return the canonical SHA-256 of a JSON-compatible value."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class NoIOContract(BaseModel):
    """The immutable zero-side-effect contract for explanation reads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_http_requests: int = Field(ge=0, le=0)
    alfred_requests: int = Field(ge=0, le=0)
    pit_materializations: int = Field(ge=0, le=0)
    provider_refreshes: int = Field(ge=0, le=0)
    current_refreshes: int = Field(ge=0, le=0)
    calendar_network_lookups: int = Field(ge=0, le=0)
    llm_requests_for_snapshot: int = Field(ge=0, le=0)


class ExplanationAuthority(BaseModel):
    """Unique versioned authority for the non-economic explanation layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    agent_version: str
    required_source_registry_version: str
    required_source_registry_hash: str
    required_current_freshness_version: str
    required_current_freshness_hash: str
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
    required_pit_materialization_version: str
    required_pit_materialization_hash: str
    required_revision_provenance_version: str
    required_revision_provenance_hash: str
    expected_pillar_ids: tuple[str, ...]
    top_n: int = Field(gt=0)
    no_io_contract: NoIOContract
    specification_hash: str
    prompt_version: str
    prompt_hash: str
    prompt_text: str

    @model_validator(mode="after")
    def validate_inventory(self) -> ExplanationAuthority:
        """Require a canonical complete nine-pillar inventory."""
        if len(self.expected_pillar_ids) != 9 or len(set(self.expected_pillar_ids)) != 9:
            raise ValueError("EXPLANATION_PILLAR_INVENTORY_MISMATCH")
        if tuple(sorted(self.expected_pillar_ids)) != self.expected_pillar_ids:
            raise ValueError("EXPLANATION_PILLARS_MUST_BE_SORTED")
        return self


def load_explanation_authority(
    path: Path = DEFAULT_EXPLANATION_CONFIG_PATH,
    *,
    prompt_path: Path = DEFAULT_EXPLANATION_PROMPT_PATH,
) -> ExplanationAuthority:
    """Load and validate the explanation authority against every frozen upstream."""
    raw_value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw_value, dict):
        raise ValueError("Explanation configuration must be a mapping")
    raw: dict[str, Any] = raw_value
    if raw.get("agent_version") != "agent-explanation-v1":
        raise ValueError("EXPLANATION_VERSION_MISMATCH")

    registry = load_source_registry()
    freshness = load_current_freshness_catalog(source_registry=registry)
    features = load_feature_catalog()
    signals = load_signal_catalog(feature_catalog=features)
    pillars = load_pillar_catalog(source_registry=registry, signal_catalog=signals)
    composite = load_composite_catalog(pillar_catalog=pillars)
    regime = load_regime_catalog(composite_catalog=composite)
    materialization = load_pit_materialization_catalog(source_registry=registry)
    revision = load_revision_provenance_catalog(
        source_registry=registry, materialization=materialization
    )
    bindings = {
        "required_source_registry_version": registry.registry_policy_version,
        "required_source_registry_hash": registry.registry_hash,
        "required_current_freshness_version": freshness.current_freshness_version,
        "required_current_freshness_hash": freshness.current_freshness_hash,
        "required_feature_spec_version": features.feature_spec_version,
        "required_feature_spec_hash": features.specification_hash,
        "required_normalization_spec_version": signals.normalization_spec_version,
        "required_normalization_spec_hash": signals.specification_hash,
        "required_pillar_spec_version": pillars.pillar_spec_version,
        "required_pillar_spec_hash": pillars.specification_hash,
        "required_composite_spec_version": composite.composite_spec_version,
        "required_composite_spec_hash": composite.specification_hash,
        "required_regime_spec_version": regime.regime_spec_version,
        "required_regime_spec_hash": regime.specification_hash,
        "required_pit_materialization_version": materialization.pit_materialization_spec_version,
        "required_pit_materialization_hash": materialization.specification_hash,
        "required_revision_provenance_version": revision.revision_provenance_version,
        "required_revision_provenance_hash": revision.specification_hash,
    }
    for key, expected in bindings.items():
        if raw.get(key) != expected:
            raise ValueError(f"EXPLANATION_BINDING_MISMATCH:{key}")
    expected_pillars = tuple(sorted(pillars.by_pillar()))
    if tuple(raw.get("expected_pillar_ids", ())) != expected_pillars:
        raise ValueError("EXPLANATION_PILLAR_BINDING_MISMATCH")

    prompt_text = prompt_path.read_text(encoding="utf-8")
    return ExplanationAuthority.model_validate(
        {
            **raw,
            "specification_hash": hash_payload(raw),
            "prompt_version": "agent-explanation-prompt-v1",
            "prompt_hash": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            "prompt_text": prompt_text,
        }
    )
