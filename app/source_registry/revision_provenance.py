"""Deterministic revision evidence derived from immutable PIT information sets."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import PitChangeIndexGeneration, PitInformationSet
from app.source_registry.information_sets import (
    find_information_set,
    information_set_rows,
    verify_information_set,
)
from app.source_registry.materialization import PitMaterializationCatalog
from app.source_registry.revision_schemas import (
    CanonicalObservationStatus,
    ObservationRevisionDelta,
    ObservationSetRevisionEnvelope,
    RevisionEffectType,
    RevisionEvidenceStatus,
    RevisionLayerEffect,
    RevisionPredecessorKind,
)

if TYPE_CHECKING:
    from app.source_registry.schemas import SourceRegistryCatalog

DEFAULT_REVISION_PROVENANCE_PATH = Path("config/data_sources/revision_provenance.yaml")
_MAP_CACHE_MAX_ENTRIES = 256
_ENVELOPE_CACHE_MAX_ENTRIES = 512
_CANONICAL_MAP_CACHE: OrderedDict[
    str, dict[date, tuple[CanonicalObservationStatus, Decimal | None]]
] = OrderedDict()
_ENVELOPE_CACHE: OrderedDict[tuple[str, str, str, str, str], ObservationSetRevisionEnvelope] = (
    OrderedDict()
)


class PredecessorAuthority(BaseModel):
    """Configured canonical predecessor rules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    same_date_correction: Literal["EXPLICIT_SUPERSESSION_LINK_ONLY"]
    previous_information_set: Literal["IMMEDIATE_PREVIOUS_CHANGE_DATE_SAME_GENERATION"]
    missing_predecessor: Literal["NO_COMPARABLE_PREDECESSOR"]
    prohibit_runtime_inference: Literal[True]


class ComparisonAuthority(BaseModel):
    """Configured observation-envelope comparison rules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_order: Literal["OBSERVATION_DATE_ASCENDING"]
    compare_canonical_values: Literal[True]
    compare_presence: Literal[True]
    compare_missing_status: Literal[True]
    new_information_is_revision: Literal[False]


class PropagationAuthority(BaseModel):
    """Configured Stage 3 propagation semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage3a: Literal["CANONICAL_COUNTERFACTUAL_RESULT"]
    stage3b: Literal["CANONICAL_COUNTERFACTUAL_RESULT_AND_DISTRIBUTION"]
    stage3c: Literal["CANONICAL_COUNTERFACTUAL_RESULT"]
    stage3d: Literal["DETERMINISTIC_UPSTREAM_EFFECTS"]
    stage3e: Literal["DETERMINISTIC_UPSTREAM_EFFECTS"]


class RevisionHashAuthority(BaseModel):
    """Allow-listed semantic and prohibited runtime hash inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    include_authority_hash: Literal[True]
    include_information_set_hashes: Literal[True]
    include_canonical_diff_hash: Literal[True]
    include_typed_effects: Literal[True]
    exclude_runtime_state: Literal[True]
    exclude_cache_state: Literal[True]
    exclude_timestamps: Literal[True]
    exclude_row_ids: Literal[True]


class CurrentPathAuthority(BaseModel):
    """Keep current resolution isolated from PIT revision semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    isolated_from_pit_revision_provenance: Literal[True]
    runtime_diagnostic_enabled: Literal[False]


class RevisionProvenanceCatalog(BaseModel):
    """Validated independent revision-provenance authority."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    revision_provenance_version: Literal["revision-provenance-v2"]
    required_source_registry_version: str
    required_source_registry_hash: str
    required_pit_materialization_version: str
    required_pit_materialization_hash: str
    predecessor: PredecessorAuthority
    comparison: ComparisonAuthority
    propagation: PropagationAuthority
    hash_policy: RevisionHashAuthority
    current_path: CurrentPathAuthority
    source_path: Path
    specification_hash: str


class RevisionProvenanceError(RuntimeError):
    """Persisted lineage cannot satisfy the frozen DLCR-004 contract."""


def clear_revision_provenance_caches() -> None:
    """Clear bounded pure memoization between independent validation lifecycles."""
    _CANONICAL_MAP_CACHE.clear()
    _ENVELOPE_CACHE.clear()


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def load_revision_provenance_catalog(
    path: Path = DEFAULT_REVISION_PROVENANCE_PATH,
    *,
    source_registry: SourceRegistryCatalog | None = None,
    materialization: PitMaterializationCatalog | None = None,
) -> RevisionProvenanceCatalog:
    """Load and hash DLCR-004 authority, failing closed on upstream drift."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Revision provenance configuration must be a mapping")
    catalog = RevisionProvenanceCatalog.model_validate(
        {**raw, "source_path": path, "specification_hash": _hash(raw)}
    )
    if source_registry is not None and (
        catalog.required_source_registry_version != source_registry.registry_policy_version
        or catalog.required_source_registry_hash != source_registry.registry_hash
    ):
        raise ValueError("REVISION_PROVENANCE_SOURCE_REGISTRY_DRIFT")
    if materialization is not None and (
        catalog.required_pit_materialization_version
        != materialization.pit_materialization_spec_version
        or catalog.required_pit_materialization_hash != materialization.specification_hash
    ):
        raise ValueError("REVISION_PROVENANCE_PIT_MATERIALIZATION_DRIFT")
    return catalog


def _same_date_predecessor(
    session: Session,
    current: PitInformationSet,
) -> PitInformationSet | None:
    rows = list(
        session.scalars(
            select(PitInformationSet).where(
                PitInformationSet.superseded_by_id == current.id,
                PitInformationSet.indicator_id == current.indicator_id,
                PitInformationSet.provider == current.provider,
                PitInformationSet.provider_series_id == current.provider_series_id,
                PitInformationSet.source_information_set_date
                == current.source_information_set_date,
                PitInformationSet.source_registry_hash == current.source_registry_hash,
                PitInformationSet.pit_materialization_spec_hash
                == current.pit_materialization_spec_hash,
            )
        ).all()
    )
    if len(rows) > 1:
        raise RevisionProvenanceError("MULTIPLE_EXPLICIT_SAME_DATE_PREDECESSORS")
    return rows[0] if rows else None


def _previous_change_date_predecessor(
    session: Session,
    generation: PitChangeIndexGeneration,
    current: PitInformationSet,
    *,
    materialization: PitMaterializationCatalog,
) -> PitInformationSet | None:
    dates = tuple(date.fromisoformat(item) for item in generation.change_dates_json)
    earlier = [item for item in dates if item < current.source_information_set_date]
    if not earlier:
        return None
    return find_information_set(
        session,
        indicator_id=current.indicator_id,
        provider_series_id=current.provider_series_id,
        source_information_set_date=earlier[-1],
        source_registry_hash=current.source_registry_hash,
        materialization=materialization,
    )


def _canonical_map(
    session: Session,
    information_set: PitInformationSet,
) -> dict[date, tuple[CanonicalObservationStatus, Decimal | None]]:
    cached = _CANONICAL_MAP_CACHE.get(information_set.information_set_hash)
    if cached is not None:
        _CANONICAL_MAP_CACHE.move_to_end(information_set.information_set_hash)
        return dict(cached)
    start = information_set.earliest_observation_date or information_set.source_information_set_date
    end = information_set.latest_observation_date or information_set.source_information_set_date
    rows = information_set_rows(session, information_set, start=start, end=end)
    result: dict[date, tuple[CanonicalObservationStatus, Decimal | None]] = {
        item.observation_date: (CanonicalObservationStatus.AVAILABLE, Decimal(item.current_value))
        for item in rows
    }
    for missing in information_set.missing_observation_dates_json:
        missing_date = date.fromisoformat(str(missing))
        if missing_date in result:
            raise RevisionProvenanceError("INFORMATION_SET_DATE_HAS_VALUE_AND_MISSING_STATUS")
        result[missing_date] = (CanonicalObservationStatus.MISSING, None)
    _CANONICAL_MAP_CACHE[information_set.information_set_hash] = dict(result)
    _CANONICAL_MAP_CACHE.move_to_end(information_set.information_set_hash)
    while len(_CANONICAL_MAP_CACHE) > _MAP_CACHE_MAX_ENTRIES:
        _CANONICAL_MAP_CACHE.popitem(last=False)
    return result


def _delta(
    observation_date: date,
    prior: tuple[CanonicalObservationStatus, Decimal | None] | None,
    current: tuple[CanonicalObservationStatus, Decimal | None] | None,
) -> ObservationRevisionDelta | None:
    prior_status, prior_value = prior or (CanonicalObservationStatus.ABSENT, None)
    current_status, current_value = current or (CanonicalObservationStatus.ABSENT, None)
    if prior_status == current_status and prior_value == current_value:
        return None
    if prior_status is CanonicalObservationStatus.ABSENT:
        effect = RevisionEffectType.NEW_INFORMATION
    elif current_status is CanonicalObservationStatus.ABSENT:
        effect = RevisionEffectType.OBSERVATION_REMOVAL
    elif prior_status != current_status:
        effect = RevisionEffectType.OBSERVATION_STATUS_CHANGE
    else:
        effect = RevisionEffectType.SOURCE_REVISION
    return ObservationRevisionDelta(
        observation_date=observation_date,
        prior_status=prior_status,
        current_status=current_status,
        prior_value=prior_value,
        current_value=current_value,
        effect_type=effect,
    )


def build_revision_envelope(
    session: Session,
    *,
    generation: PitChangeIndexGeneration,
    current: PitInformationSet,
    unit: str,
    frequency: str,
    materialization: PitMaterializationCatalog,
    authority: RevisionProvenanceCatalog,
) -> ObservationSetRevisionEnvelope:
    """Compare one PIT information set with its explicit canonical predecessor."""
    if generation.generation_hash == "" or current.information_set_hash == "":
        raise RevisionProvenanceError("PERSISTED_IDENTITY_REQUIRED")
    cache_key = (
        authority.specification_hash,
        generation.generation_hash,
        current.information_set_hash,
        unit,
        frequency,
    )
    cached = _ENVELOPE_CACHE.get(cache_key)
    if cached is not None:
        _ENVELOPE_CACHE.move_to_end(cache_key)
        return cached.model_copy(deep=True)
    predecessor = _same_date_predecessor(session, current)
    kind = RevisionPredecessorKind.SAME_DATE_CORRECTION
    if predecessor is None:
        predecessor = _previous_change_date_predecessor(
            session, generation, current, materialization=materialization
        )
        kind = (
            RevisionPredecessorKind.PREVIOUS_CHANGE_DATE
            if predecessor is not None
            else RevisionPredecessorKind.NO_PREDECESSOR
        )
    verify_information_set(
        session, current, unit=unit, frequency=frequency, materialization=materialization
    )
    if predecessor is not None:
        verify_information_set(
            session,
            predecessor,
            unit=unit,
            frequency=frequency,
            materialization=materialization,
        )
    current_map = _canonical_map(session, current)
    predecessor_map = _canonical_map(session, predecessor) if predecessor is not None else {}
    deltas: tuple[ObservationRevisionDelta, ...]
    if predecessor is None:
        deltas = ()
    else:
        deltas = tuple(
            delta
            for observation_date in sorted(set(predecessor_map) | set(current_map))
            if (
                delta := _delta(
                    observation_date,
                    predecessor_map.get(observation_date),
                    current_map.get(observation_date),
                )
            )
            is not None
        )
    effect_types = tuple(sorted({item.effect_type for item in deltas}, key=str))
    revision_types = {
        RevisionEffectType.SOURCE_REVISION,
        RevisionEffectType.OBSERVATION_REMOVAL,
        RevisionEffectType.OBSERVATION_STATUS_CHANGE,
    }
    if predecessor is None:
        evidence_status = RevisionEvidenceStatus.NO_COMPARABLE_PREDECESSOR
    elif revision_types.intersection(effect_types):
        evidence_status = RevisionEvidenceStatus.REVISION_DETECTED
    else:
        evidence_status = RevisionEvidenceStatus.NO_REVISION_DETECTED
    delta_payload = [item.model_dump(mode="json") for item in deltas]
    semantic_diff_hash = _hash(delta_payload)
    base = {
        "revision_provenance_version": authority.revision_provenance_version,
        "revision_provenance_hash": authority.specification_hash,
        "indicator_id": current.indicator_id,
        "provider": current.provider,
        "provider_series_id": current.provider_series_id,
        "index_generation_hash": generation.generation_hash,
        "current_information_set_hash": current.information_set_hash,
        "predecessor_information_set_hash": (
            predecessor.information_set_hash if predecessor is not None else None
        ),
        "current_source_information_set_date": current.source_information_set_date,
        "predecessor_source_information_set_date": (
            predecessor.source_information_set_date if predecessor is not None else None
        ),
        "predecessor_kind": kind,
        "current_observation_count": current.observation_count,
        "predecessor_observation_count": predecessor.observation_count if predecessor else 0,
        "added_observation_dates": tuple(
            item.observation_date
            for item in deltas
            if item.effect_type is RevisionEffectType.NEW_INFORMATION
        ),
        "removed_observation_dates": tuple(
            item.observation_date
            for item in deltas
            if item.effect_type is RevisionEffectType.OBSERVATION_REMOVAL
        ),
        "revised_value_dates": tuple(
            item.observation_date
            for item in deltas
            if item.effect_type is RevisionEffectType.SOURCE_REVISION
        ),
        "status_changed_dates": tuple(
            item.observation_date
            for item in deltas
            if item.effect_type is RevisionEffectType.OBSERVATION_STATUS_CHANGE
        ),
        "observation_deltas": deltas,
        "canonical_semantic_diff_hash": semantic_diff_hash,
        "effect_types": effect_types,
        "evidence_status": evidence_status,
    }
    envelope = ObservationSetRevisionEnvelope(
        **base,
        revision_evidence_hash=_hash(
            {
                **base,
                "observation_deltas": delta_payload,
                "effect_types": [item.value for item in effect_types],
                "predecessor_kind": kind.value,
                "evidence_status": evidence_status.value,
            }
        ),
    )
    _ENVELOPE_CACHE[cache_key] = envelope.model_copy(deep=True)
    _ENVELOPE_CACHE.move_to_end(cache_key)
    while len(_ENVELOPE_CACHE) > _ENVELOPE_CACHE_MAX_ENTRIES:
        _ENVELOPE_CACHE.popitem(last=False)
    return envelope


def build_layer_effect(
    *,
    authority: RevisionProvenanceCatalog,
    source_evidence_hashes: tuple[str, ...],
    source_effect_types: tuple[RevisionEffectType, ...],
    current_value: Decimal | None,
    counterfactual_value: Decimal | None,
    current_status: str,
    counterfactual_status: str,
    distribution_changed: bool = False,
    result_changed_override: bool | None = None,
    current_result_payload: object | None = None,
    counterfactual_result_payload: object | None = None,
) -> RevisionLayerEffect:
    """Create a stable layer effect from canonical current and counterfactual outputs."""
    inferred_result_changed = (
        current_value != counterfactual_value or current_status != counterfactual_status
    )
    result_changed = (
        inferred_result_changed if result_changed_override is None else result_changed_override
    )
    effects = set(source_effect_types)
    if distribution_changed:
        effects.add(RevisionEffectType.DISTRIBUTION_CHANGE)
    if result_changed:
        effects.add(RevisionEffectType.RESULT_CHANGE)
    current_hash = _hash(
        current_result_payload
        if current_result_payload is not None
        else {"value": current_value, "status": current_status}
    )
    counterfactual_hash = _hash(
        counterfactual_result_payload
        if counterfactual_result_payload is not None
        else {"value": counterfactual_value, "status": counterfactual_status}
    )
    evidence_hashes = tuple(sorted(set(source_evidence_hashes)))
    source_effects = tuple(sorted(set(source_effect_types), key=str))
    effect_types = tuple(sorted(effects, key=str))
    payload = {
        "revision_provenance_version": authority.revision_provenance_version,
        "revision_provenance_hash": authority.specification_hash,
        "source_revision_evidence_hashes": evidence_hashes,
        "source_effect_types": source_effects,
        "effect_types": effect_types,
        "current_result_hash": current_hash,
        "counterfactual_result_hash": counterfactual_hash,
        "result_changed": result_changed,
        "distribution_changed": distribution_changed,
    }
    return RevisionLayerEffect(
        revision_provenance_version=authority.revision_provenance_version,
        revision_provenance_hash=authority.specification_hash,
        source_revision_evidence_hashes=evidence_hashes,
        source_effect_types=source_effects,
        effect_types=effect_types,
        current_value=current_value,
        counterfactual_value=counterfactual_value,
        current_status=current_status,
        counterfactual_status=counterfactual_status,
        result_changed=result_changed,
        distribution_changed=distribution_changed,
        current_result_hash=current_hash,
        counterfactual_result_hash=counterfactual_hash,
        revision_effect_hash=_hash(payload),
    )
