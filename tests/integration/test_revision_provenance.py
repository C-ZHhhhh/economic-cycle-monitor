"""PostgreSQL tests for canonical DLCR-004 information-set revision evidence."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.data_sources.fred.schemas import FredObservation
from app.database.models import PitChangeIndexGeneration, PitInformationSet
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.source_registry.config import load_source_registry
from app.source_registry.information_sets import persist_information_set
from app.source_registry.materialization import (
    PitMaterializationCatalog,
    load_pit_materialization_catalog,
)
from app.source_registry.pit_index import ChangeIndexDiscovery, persist_change_index_generation
from app.source_registry.resolver import get_effective_observations
from app.source_registry.revision_provenance import (
    RevisionProvenanceCatalog,
    build_revision_envelope,
    clear_revision_provenance_caches,
    load_revision_provenance_catalog,
)
from app.source_registry.revision_schemas import (
    ObservationSetRevisionEnvelope,
    RevisionEffectType,
    RevisionEvidenceStatus,
    RevisionPredecessorKind,
)
from app.source_registry.schemas import SourceRegistryCatalog

pytestmark = pytest.mark.integration

FIRST = date(2020, 6, 28)
SECOND = date(2020, 7, 19)
COVERAGE_END = date(2020, 7, 20)


def _observation(day: int, value: str | None, *, source_date: date) -> FredObservation:
    return FredObservation(
        realtime_start=source_date,
        realtime_end=source_date,
        date=date(2020, day, 1),
        value=Decimal(value) if value is not None else None,
    )


def _setup(
    session: Session,
    *,
    change_dates: tuple[date, ...] = (FIRST, SECOND),
) -> tuple[
    SourceRegistryCatalog,
    PitMaterializationCatalog,
    RevisionProvenanceCatalog,
    PitChangeIndexGeneration,
]:
    registry = load_source_registry()
    seed_indicator_catalog(session, load_indicator_catalog(), registry)
    materialization = load_pit_materialization_catalog(source_registry=registry)
    authority = load_revision_provenance_catalog(
        source_registry=registry, materialization=materialization
    )
    generation = persist_change_index_generation(
        session,
        indicator_id="US_REAL_GDP",
        provider_series_id="GDPC1",
        discovery=ChangeIndexDiscovery(
            coverage_start=FIRST,
            coverage_end=COVERAGE_END,
            complete_through=COVERAGE_END,
            change_dates=change_dates,
            provider_count=len(change_dates),
            pages=(),
            generation_hash="9" * 64,
        ),
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
        discovered_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    return registry, materialization, authority, generation


def _persist(
    session: Session,
    *,
    source_date: date,
    observations: list[FredObservation],
    generation: PitChangeIndexGeneration,
    registry: SourceRegistryCatalog,
    materialization: PitMaterializationCatalog,
    raw_hash: str,
) -> PitInformationSet:
    return persist_information_set(
        session,
        indicator_id="US_REAL_GDP",
        provider_series_id="GDPC1",
        source_information_set_date=source_date,
        unit="chained_dollars",
        frequency="quarterly",
        observations=observations,
        raw_payload_hash=raw_hash,
        retrieved_at=datetime(2026, 9, 4, tzinfo=UTC),
        generation=generation,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
    )


def _envelope(
    session: Session,
    *,
    current: PitInformationSet,
    generation: PitChangeIndexGeneration,
    materialization: PitMaterializationCatalog,
    authority: RevisionProvenanceCatalog,
) -> ObservationSetRevisionEnvelope:
    return build_revision_envelope(
        session,
        generation=generation,
        current=current,
        unit="chained_dollars",
        frequency="quarterly",
        materialization=materialization,
        authority=authority,
    )


def test_only_added_observation_is_new_information_not_revision(
    postgres_session: Session,
) -> None:
    registry, materialization, authority, generation = _setup(postgres_session)
    _persist(
        postgres_session,
        source_date=FIRST,
        observations=[_observation(1, "100", source_date=FIRST)],
        generation=generation,
        registry=registry,
        materialization=materialization,
        raw_hash="a" * 64,
    )
    current = _persist(
        postgres_session,
        source_date=SECOND,
        observations=[
            _observation(1, "100", source_date=SECOND),
            _observation(4, "101", source_date=SECOND),
        ],
        generation=generation,
        registry=registry,
        materialization=materialization,
        raw_hash="b" * 64,
    )
    evidence = _envelope(
        postgres_session,
        current=current,
        generation=generation,
        materialization=materialization,
        authority=authority,
    )

    assert evidence.effect_types == (RevisionEffectType.NEW_INFORMATION,)
    assert evidence.added_observation_dates == (date(2020, 4, 1),)
    assert evidence.has_canonical_revision is False
    assert evidence.evidence_status is RevisionEvidenceStatus.NO_REVISION_DETECTED


@pytest.mark.parametrize(
    ("prior_rows", "current_rows", "expected_effect", "date_field"),
    [
        (
            [(1, "100"), (4, "200")],
            [(1, "101"), (4, "200")],
            RevisionEffectType.SOURCE_REVISION,
            "revised_value_dates",
        ),
        (
            [(1, "100"), (4, "200")],
            [(4, "200")],
            RevisionEffectType.OBSERVATION_REMOVAL,
            "removed_observation_dates",
        ),
        (
            [(1, None), (4, "200")],
            [(1, "100"), (4, "200")],
            RevisionEffectType.OBSERVATION_STATUS_CHANGE,
            "status_changed_dates",
        ),
    ],
)
def test_canonical_revision_effects_are_distinct(
    postgres_session: Session,
    prior_rows: list[tuple[int, str | None]],
    current_rows: list[tuple[int, str | None]],
    expected_effect: RevisionEffectType,
    date_field: str,
) -> None:
    registry, materialization, authority, generation = _setup(postgres_session)
    _persist(
        postgres_session,
        source_date=FIRST,
        observations=[_observation(day, value, source_date=FIRST) for day, value in prior_rows],
        generation=generation,
        registry=registry,
        materialization=materialization,
        raw_hash="a" * 64,
    )
    current = _persist(
        postgres_session,
        source_date=SECOND,
        observations=[_observation(day, value, source_date=SECOND) for day, value in current_rows],
        generation=generation,
        registry=registry,
        materialization=materialization,
        raw_hash="b" * 64,
    )
    evidence = _envelope(
        postgres_session,
        current=current,
        generation=generation,
        materialization=materialization,
        authority=authority,
    )

    assert evidence.effect_types == (expected_effect,)
    assert getattr(evidence, date_field) == (date(2020, 1, 1),)
    assert evidence.has_canonical_revision is True
    assert evidence.evidence_status is RevisionEvidenceStatus.REVISION_DETECTED


def test_same_date_correction_uses_explicit_supersession_predecessor(
    postgres_session: Session,
) -> None:
    registry, materialization, authority, generation = _setup(
        postgres_session, change_dates=(FIRST,)
    )
    prior = _persist(
        postgres_session,
        source_date=FIRST,
        observations=[_observation(1, "100", source_date=FIRST)],
        generation=generation,
        registry=registry,
        materialization=materialization,
        raw_hash="a" * 64,
    )
    current = _persist(
        postgres_session,
        source_date=FIRST,
        observations=[_observation(1, "101", source_date=FIRST)],
        generation=generation,
        registry=registry,
        materialization=materialization,
        raw_hash="b" * 64,
    )
    evidence = _envelope(
        postgres_session,
        current=current,
        generation=generation,
        materialization=materialization,
        authority=authority,
    )

    assert prior.superseded_by_id == current.id
    assert evidence.predecessor_kind is RevisionPredecessorKind.SAME_DATE_CORRECTION
    assert evidence.predecessor_information_set_hash == prior.information_set_hash
    assert evidence.effect_types == (RevisionEffectType.SOURCE_REVISION,)


def test_no_predecessor_is_typed_and_does_not_guess(postgres_session: Session) -> None:
    registry, materialization, authority, generation = _setup(
        postgres_session, change_dates=(FIRST,)
    )
    current = _persist(
        postgres_session,
        source_date=FIRST,
        observations=[_observation(1, "100", source_date=FIRST)],
        generation=generation,
        registry=registry,
        materialization=materialization,
        raw_hash="a" * 64,
    )
    evidence = _envelope(
        postgres_session,
        current=current,
        generation=generation,
        materialization=materialization,
        authority=authority,
    )

    assert evidence.predecessor_kind is RevisionPredecessorKind.NO_PREDECESSOR
    assert evidence.evidence_status is RevisionEvidenceStatus.NO_COMPARABLE_PREDECESSOR
    assert evidence.observation_deltas == ()
    assert evidence.effect_types == ()


def test_evaluation_progression_with_same_information_set_keeps_identical_evidence(
    postgres_session: Session,
) -> None:
    registry, materialization, _, generation = _setup(postgres_session)
    _persist(
        postgres_session,
        source_date=FIRST,
        observations=[_observation(1, "100", source_date=FIRST)],
        generation=generation,
        registry=registry,
        materialization=materialization,
        raw_hash="a" * 64,
    )
    _persist(
        postgres_session,
        source_date=SECOND,
        observations=[
            _observation(1, "100", source_date=SECOND),
            _observation(4, "101", source_date=SECOND),
        ],
        generation=generation,
        registry=registry,
        materialization=materialization,
        raw_hash="b" * 64,
    )
    first = get_effective_observations(
        postgres_session,
        "US_REAL_GDP",
        date(2020, 1, 1),
        date(2020, 4, 1),
        SECOND,
        registry=registry,
        materialization=materialization,
    )
    later = get_effective_observations(
        postgres_session,
        "US_REAL_GDP",
        date(2020, 1, 1),
        date(2020, 4, 1),
        SECOND.fromordinal(SECOND.toordinal() + 1),
        registry=registry,
        materialization=materialization,
    )

    assert first[0].revision_evidence == later[0].revision_evidence
    assert first[0].revision_evidence is not None
    assert first[0].revision_evidence.has_canonical_revision is False


def test_revision_evidence_is_identical_with_cold_and_warm_caches(
    postgres_session: Session,
) -> None:
    registry, materialization, authority, generation = _setup(postgres_session)
    _persist(
        postgres_session,
        source_date=FIRST,
        observations=[_observation(1, "100", source_date=FIRST)],
        generation=generation,
        registry=registry,
        materialization=materialization,
        raw_hash="a" * 64,
    )
    current = _persist(
        postgres_session,
        source_date=SECOND,
        observations=[_observation(1, "101", source_date=SECOND)],
        generation=generation,
        registry=registry,
        materialization=materialization,
        raw_hash="b" * 64,
    )
    clear_revision_provenance_caches()
    cold = _envelope(
        postgres_session,
        current=current,
        generation=generation,
        materialization=materialization,
        authority=authority,
    )
    warm = _envelope(
        postgres_session,
        current=current,
        generation=generation,
        materialization=materialization,
        authority=authority,
    )
    clear_revision_provenance_caches()
    fresh = _envelope(
        postgres_session,
        current=current,
        generation=generation,
        materialization=materialization,
        authority=authority,
    )

    assert cold == warm == fresh
    assert cold.revision_evidence_hash == fresh.revision_evidence_hash
