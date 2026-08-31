"""Immutable semantic ALFRED information-set identities and observation persistence."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_sources.fred.schemas import FredObservation
from app.database.models import ObservationVintage, PitChangeIndexGeneration, PitInformationSet
from app.source_registry.materialization import PitMaterializationCatalog


class InformationSetStatus(StrEnum):
    """Lifecycle of one immutable semantic payload."""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    MALFORMED = "MALFORMED"
    HASH_MISMATCH = "HASH_MISMATCH"


class InformationSetStorageMode(StrEnum):
    """Physical storage mode without changing semantic identity."""

    SEMANTIC_LINKED = "SEMANTIC_LINKED"
    LEGACY_EXACT = "LEGACY_EXACT"


class InformationSetError(RuntimeError):
    """Base semantic information-set error."""


class InformationSetNotFound(InformationSetError):
    """The selected source information set has not been materialized."""


class InformationSetMalformed(InformationSetError):
    """The provider payload cannot form one deterministic information set."""


class InformationSetHashMismatch(InformationSetError):
    """Persisted observations no longer match their immutable header."""


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def canonical_observations(observations: list[FredObservation]) -> list[dict[str, str | None]]:
    """Return deterministic values and explicit missing statuses sorted by economic date."""
    dates = [item.date for item in observations]
    if len(dates) != len(set(dates)):
        raise InformationSetMalformed("PIT_INFORMATION_SET_MALFORMED:DUPLICATE_DATE")
    return [
        {
            "observation_date": item.date.isoformat(),
            "value": _canonical_decimal(item.value) if item.value is not None else None,
            "status": "AVAILABLE" if item.value is not None else "MISSING",
        }
        for item in sorted(observations, key=lambda item: item.date)
    ]


def information_set_hash(
    *,
    indicator_id: str,
    provider_series_id: str,
    source_information_set_date: date,
    unit: str,
    frequency: str,
    source_registry_hash: str,
    materialization: PitMaterializationCatalog,
    observations: list[FredObservation],
) -> str:
    """Hash official identity and canonical semantic observation payload."""
    official_identity = f"ALFRED_VINTAGE:{provider_series_id}:END_OF_DAY"
    return _hash(
        {
            "indicator_id": indicator_id,
            "provider": materialization.information_set.provider,
            "provider_series_id": provider_series_id,
            "source_information_set_date": source_information_set_date,
            "source_registry_hash": source_registry_hash,
            "pit_materialization_spec_hash": materialization.specification_hash,
            "official_source_contract_identity": official_identity,
            "canonical_unit": unit,
            "canonical_frequency": frequency,
            "canonical_observations": canonical_observations(observations),
        }
    )


def evaluation_result_hash(
    *,
    evaluation_as_of: date,
    information_hash: str,
    start: date,
    end: date,
    materialization: PitMaterializationCatalog,
) -> str:
    """Identify one evaluation context separately from its reusable source snapshot."""
    return _hash(
        {
            "calculation_provenance_version": materialization.calculation_provenance.version,
            "calculation_provenance_hash": materialization.calculation_provenance_hash,
            "evaluation_as_of": evaluation_as_of,
            "information_set_hash": information_hash,
            "pit_materialization_spec_hash": materialization.specification_hash,
            "requested_start": start,
            "requested_end": end,
            "resolver_contract": materialization.resolver.lookup,
        }
    )


def find_information_set(
    session: Session,
    *,
    indicator_id: str,
    provider_series_id: str,
    source_information_set_date: date,
    source_registry_hash: str,
    materialization: PitMaterializationCatalog,
) -> PitInformationSet | None:
    """Return the newest active semantic identity for one official change date."""
    return session.scalar(
        select(PitInformationSet)
        .where(
            PitInformationSet.indicator_id == indicator_id,
            PitInformationSet.provider == materialization.information_set.provider,
            PitInformationSet.provider_series_id == provider_series_id,
            PitInformationSet.source_information_set_date == source_information_set_date,
            PitInformationSet.source_registry_hash == source_registry_hash,
            PitInformationSet.pit_materialization_spec_hash == materialization.specification_hash,
            PitInformationSet.status == InformationSetStatus.ACTIVE.value,
        )
        .order_by(PitInformationSet.id.desc())
        .limit(1)
    )


def information_set_rows(
    session: Session,
    information_set: PitInformationSet,
    *,
    start: date,
    end: date,
) -> list[ObservationVintage]:
    """Load linked semantic rows or one explicitly registered legacy exact snapshot."""
    statement = select(ObservationVintage).where(
        ObservationVintage.indicator_id == information_set.indicator_id,
        ObservationVintage.source == "fred",
        ObservationVintage.observation_date.between(start, end),
    )
    if information_set.storage_mode == InformationSetStorageMode.SEMANTIC_LINKED.value:
        statement = statement.where(ObservationVintage.pit_information_set_id == information_set.id)
    elif (
        information_set.storage_mode == InformationSetStorageMode.LEGACY_EXACT.value
        and information_set.legacy_vintage_date is not None
    ):
        statement = statement.where(
            ObservationVintage.vintage_date == information_set.legacy_vintage_date,
            ObservationVintage.release_stage == "as_known_on",
            ObservationVintage.raw_payload_hash == information_set.raw_payload_hash,
        )
    else:
        raise InformationSetMalformed("PIT_INFORMATION_SET_MALFORMED:STORAGE_MODE")
    return list(session.scalars(statement.order_by(ObservationVintage.observation_date)).all())


def persist_information_set(
    session: Session,
    *,
    indicator_id: str,
    provider_series_id: str,
    source_information_set_date: date,
    unit: str,
    frequency: str,
    observations: list[FredObservation],
    raw_payload_hash: str,
    retrieved_at: datetime,
    generation: PitChangeIndexGeneration,
    source_registry_hash: str,
    materialization: PitMaterializationCatalog,
    sync_run_id: UUID | None = None,
) -> PitInformationSet:
    """Persist one immutable source snapshot and retain same-date correction lineage."""
    visible = [item for item in observations if item.date <= source_information_set_date]
    canonical = canonical_observations(visible)
    if not any(item["status"] == "AVAILABLE" for item in canonical):
        raise InformationSetMalformed("PIT_EMPTY_INFORMATION_SET")
    semantic_hash = information_set_hash(
        indicator_id=indicator_id,
        provider_series_id=provider_series_id,
        source_information_set_date=source_information_set_date,
        unit=unit,
        frequency=frequency,
        source_registry_hash=source_registry_hash,
        materialization=materialization,
        observations=visible,
    )
    existing = session.scalar(
        select(PitInformationSet).where(
            PitInformationSet.indicator_id == indicator_id,
            PitInformationSet.provider == materialization.information_set.provider,
            PitInformationSet.provider_series_id == provider_series_id,
            PitInformationSet.source_information_set_date == source_information_set_date,
            PitInformationSet.source_registry_hash == source_registry_hash,
            PitInformationSet.pit_materialization_spec_hash == materialization.specification_hash,
            PitInformationSet.information_set_hash == semantic_hash,
        )
    )
    if existing is not None:
        return existing
    active = find_information_set(
        session,
        indicator_id=indicator_id,
        provider_series_id=provider_series_id,
        source_information_set_date=source_information_set_date,
        source_registry_hash=source_registry_hash,
        materialization=materialization,
    )
    values = [item for item in visible if item.value is not None]
    missing_dates = [item.date.isoformat() for item in visible if item.value is None]
    record = PitInformationSet(
        indicator_id=indicator_id,
        provider=materialization.information_set.provider,
        provider_series_id=provider_series_id,
        source_information_set_date=source_information_set_date,
        information_set_hash=semantic_hash,
        official_source_contract_identity=(f"ALFRED_VINTAGE:{provider_series_id}:END_OF_DAY"),
        source_registry_hash=source_registry_hash,
        pit_materialization_spec_version=materialization.pit_materialization_spec_version,
        pit_materialization_spec_hash=materialization.specification_hash,
        index_generation_id=generation.id,
        status=InformationSetStatus.ACTIVE.value,
        storage_mode=InformationSetStorageMode.SEMANTIC_LINKED.value,
        observation_count=len(values),
        missing_observation_dates_json=missing_dates,
        earliest_observation_date=min((item.date for item in values), default=None),
        latest_observation_date=max((item.date for item in values), default=None),
        raw_payload_hash=raw_payload_hash,
        retrieved_at=retrieved_at,
        sync_run_id=sync_run_id,
    )
    session.add(record)
    session.flush()
    value_dates = [item.date for item in values]
    max_revisions: dict[date, int] = {
        observation_date: int(revision_number)
        for observation_date, revision_number in session.execute(
            select(
                ObservationVintage.observation_date,
                func.coalesce(func.max(ObservationVintage.revision_number), 0),
            )
            .where(
                ObservationVintage.indicator_id == indicator_id,
                ObservationVintage.source == "fred",
                ObservationVintage.observation_date.in_(value_dates),
            )
            .group_by(ObservationVintage.observation_date)
        ).all()
    }
    for item in values:
        assert item.value is not None
        session.add(
            ObservationVintage(
                indicator_id=indicator_id,
                observation_date=item.date,
                vintage_date=source_information_set_date,
                available_at=datetime.combine(
                    source_information_set_date, datetime.max.time(), tzinfo=UTC
                ),
                release_stage="semantic_information_set",
                revision_number=int(max_revisions.get(item.date, 0)) + 1,
                current_value=item.value,
                source="fred",
                source_vintage_date=source_information_set_date,
                retrieved_at=retrieved_at,
                raw_payload_hash=raw_payload_hash,
                pit_information_set_id=record.id,
                source_details_json={
                    "information_set_semantics": "END_OF_DAY",
                    "source_information_set_date": source_information_set_date.isoformat(),
                    "information_set_hash": semantic_hash,
                    "pit_materialization_spec_version": (
                        materialization.pit_materialization_spec_version
                    ),
                    "pit_materialization_spec_hash": materialization.specification_hash,
                },
            )
        )
    if active is not None:
        active.status = InformationSetStatus.SUPERSEDED.value
        active.superseded_at = retrieved_at
        active.superseded_by_id = record.id
    session.flush()
    return record


def verify_information_set(
    session: Session,
    information_set: PitInformationSet,
    *,
    unit: str,
    frequency: str,
    materialization: PitMaterializationCatalog,
) -> None:
    """Fail closed if linked canonical values no longer reproduce the immutable hash."""
    rows = information_set_rows(
        session,
        information_set,
        start=information_set.earliest_observation_date
        or information_set.source_information_set_date,
        end=information_set.latest_observation_date or information_set.source_information_set_date,
    )
    reconstructed = [
        FredObservation(
            realtime_start=information_set.source_information_set_date,
            realtime_end=information_set.source_information_set_date,
            date=item.observation_date,
            value=Decimal(item.current_value),
        )
        for item in rows
    ]
    reconstructed.extend(
        FredObservation(
            realtime_start=information_set.source_information_set_date,
            realtime_end=information_set.source_information_set_date,
            date=date.fromisoformat(item),
            value=None,
        )
        for item in information_set.missing_observation_dates_json
    )
    actual = information_set_hash(
        indicator_id=information_set.indicator_id,
        provider_series_id=information_set.provider_series_id,
        source_information_set_date=information_set.source_information_set_date,
        unit=unit,
        frequency=frequency,
        source_registry_hash=information_set.source_registry_hash,
        materialization=materialization,
        observations=reconstructed,
    )
    if actual != information_set.information_set_hash:
        raise InformationSetHashMismatch("PIT_INFORMATION_SET_HASH_MISMATCH")
