"""Versioned authority and deterministic identities for semantic PIT materialization."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.source_registry.schemas import SourceRegistryCatalog

DEFAULT_PIT_MATERIALIZATION_PATH = Path("config/data_sources/pit_materialization.yaml")


class ChangeIndexAuthority(BaseModel):
    """Rules that make an ALFRED vintage-date index complete and authoritative."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["ALFRED_VINTAGE"]
    endpoint: Literal["series/vintagedates"]
    page_limit: int = Field(ge=1, le=10_000)
    sort_order: Literal["asc"]
    require_stable_count: Literal[True]
    require_contiguous_offsets: Literal[True]
    require_unique_dates: Literal[True]
    require_strict_date_order: Literal[True]
    require_retrieved_count_match: Literal[True]
    immutable_generations: Literal[True]


class ClosedEodAuthority(BaseModel):
    """Date-only closure rule used before an index watermark can be claimed."""

    model_config = ConfigDict(extra="forbid")

    policy: Literal["PRIOR_UTC_CALENDAR_DATE"]
    timezone: Literal["UTC"]

    def latest_closed_date(self, now: datetime | None = None) -> date:
        """Return the latest fully closed UTC calendar date."""
        current = now or datetime.now(UTC)
        if current.utcoffset() is None:
            raise ValueError("closed-EOD evaluation requires an aware datetime")
        return current.astimezone(UTC).date() - timedelta(days=1)


class InformationSetAuthority(BaseModel):
    """Stable semantic snapshot identity rules."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["ALFRED_VINTAGE"]
    endpoint: Literal["series/observations"]
    observation_sort: Literal["observation_date_ascending"]
    identity_components: list[str]
    excluded_identity_components: list[str]


class ResolverAuthority(BaseModel):
    """Fail-closed semantic lookup behavior."""

    model_config = ConfigDict(extra="forbid")

    lookup: Literal["LATEST_CHANGE_ON_OR_BEFORE_EVALUATION_EOD"]
    semantic_snapshot_reuse: Literal[True]
    exact_network_fallback: Literal[False]
    incomplete_index_behavior: Literal["FAIL_CLOSED"]
    unavailable_index_behavior: Literal["FAIL_CLOSED"]
    stale_index_behavior: Literal["REFRESH_OR_FAIL_CLOSED"]
    current_path_isolated: Literal[True]


class RefreshAuthority(BaseModel):
    """Immutable incremental refresh and precise invalidation rules."""

    model_config = ConfigDict(extra="forbid")

    stale_target_policy: Literal["INCREMENTAL_REFRESH_OR_FAIL_CLOSED"]
    generation_policy: Literal["CREATE_IMMUTABLE_GENERATION"]
    historical_reconciliation: Literal["ADD_REMOVE_CORRECTION_DIFF"]
    invalidation_policy: Literal["PRECISE_CHANGED_DATE_INTERVALS"]
    mutate_prior_generation: Literal[False]


class LegacyAuthority(BaseModel):
    """Compatibility contract for immutable DLCR-001 exact snapshots."""

    model_config = ConfigDict(extra="forbid")

    preserve_exact_snapshots: Literal[True]
    preserve_vintage_date_semantics: Literal[True]
    exact_resolver_role: Literal["BASELINE_EQUIVALENCE_COMPARATOR_ONLY"]


class CalculationProvenanceAuthority(BaseModel):
    """Independent version for PIT hash/provenance changes without formula changes."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["pit-calculation-provenance-v2"]
    source_hash_includes_evaluation_as_of: Literal[False]
    evaluation_hash_includes_evaluation_as_of: Literal[True]
    current_hash_compatibility: Literal["PRESERVE_V1"]


class BulkAuthority(BaseModel):
    """Explicitly deferred bulk-observation behavior."""

    model_config = ConfigDict(extra="forbid")

    enabled: Literal[False]
    deferred_contract: Literal["DLCR-003B"]


class ConcurrencyAuthority(BaseModel):
    """Frozen HTTP concurrency ceilings."""

    model_config = ConfigDict(extra="forbid")

    alfred_materialization: Literal[1]
    global_http_max: Literal[4]
    per_source_http_max: Literal[2]


class PitMaterializationCatalog(BaseModel):
    """Fully validated PIT materialization authority with deterministic hashes."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    pit_materialization_spec_version: Literal["pit-materialization-v2"]
    required_source_registry_version: str
    required_source_registry_hash: str
    change_index: ChangeIndexAuthority
    closed_eod: ClosedEodAuthority
    information_set: InformationSetAuthority
    resolver: ResolverAuthority
    refresh: RefreshAuthority
    legacy: LegacyAuthority
    calculation_provenance: CalculationProvenanceAuthority
    bulk_observations: BulkAuthority
    concurrency: ConcurrencyAuthority
    source_path: Path
    specification_hash: str
    calculation_provenance_hash: str

    @model_validator(mode="after")
    def validate_identity_contract(self) -> "PitMaterializationCatalog":
        """Reject overlap between semantic identity and runtime-only components."""
        overlap = set(self.information_set.identity_components) & set(
            self.information_set.excluded_identity_components
        )
        if overlap:
            raise ValueError(f"PIT identity components cannot be excluded: {sorted(overlap)}")
        required = {
            "indicator_id",
            "provider",
            "provider_series_id",
            "source_information_set_date",
            "source_registry_hash",
            "pit_materialization_spec_hash",
            "official_source_contract_identity",
            "canonical_unit",
            "canonical_frequency",
            "canonical_observations",
        }
        if set(self.information_set.identity_components) != required:
            raise ValueError("PIT information-set identity components drifted")
        return self


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_pit_materialization_catalog(
    path: Path = DEFAULT_PIT_MATERIALIZATION_PATH,
    *,
    source_registry: SourceRegistryCatalog | None = None,
) -> PitMaterializationCatalog:
    """Load PIT authority and fail closed on source-registry drift."""
    raw_value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw_value, dict):
        raise ValueError("PIT materialization configuration must be a mapping")
    hash_payload = dict(raw_value)
    specification_hash = _sha256(hash_payload)
    provenance = raw_value.get("calculation_provenance")
    if not isinstance(provenance, dict):
        raise ValueError("PIT calculation provenance authority is required")
    catalog = PitMaterializationCatalog.model_validate(
        {
            **raw_value,
            "source_path": path,
            "specification_hash": specification_hash,
            "calculation_provenance_hash": _sha256(provenance),
        }
    )
    if source_registry is not None and (
        catalog.required_source_registry_version != source_registry.registry_policy_version
        or catalog.required_source_registry_hash != source_registry.registry_hash
    ):
        raise ValueError("PIT_MATERIALIZATION_SOURCE_REGISTRY_DRIFT")
    return catalog
