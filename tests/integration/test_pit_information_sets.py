"""PostgreSQL integration tests for immutable semantic PIT information sets."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_sources.fred.schemas import FredObservation
from app.database.models import ObservationVintage, PitChangeIndexGeneration, PitInformationSet
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.source_registry.config import load_source_registry
from app.source_registry.information_sets import persist_information_set
from app.source_registry.materialization import (
    PitMaterializationCatalog,
    load_pit_materialization_catalog,
)
from app.source_registry.pit_index import ChangeIndexDiscovery, persist_change_index_generation
from app.source_registry.resolver import PITInformationSetHashMismatch, get_effective_observations
from app.source_registry.schemas import SourceRegistryCatalog

pytestmark = pytest.mark.integration


def _generation(
    session: Session,
) -> tuple[SourceRegistryCatalog, PitMaterializationCatalog, PitChangeIndexGeneration]:
    registry = load_source_registry()
    seed_indicator_catalog(session, load_indicator_catalog(), registry)
    materialization = load_pit_materialization_catalog(source_registry=registry)
    generation = persist_change_index_generation(
        session,
        indicator_id="US_REAL_GDP",
        provider_series_id="GDPC1",
        discovery=ChangeIndexDiscovery(
            coverage_start=date(1776, 7, 4),
            coverage_end=date(2020, 7, 19),
            complete_through=date(2020, 7, 19),
            change_dates=(date(2020, 6, 28),),
            provider_count=1,
            pages=(),
            generation_hash="1" * 64,
        ),
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
        discovered_at=datetime(2026, 8, 31, tzinfo=UTC),
    )
    return registry, materialization, generation


def _observations(value: str) -> list[FredObservation]:
    return [
        FredObservation(
            realtime_start=date(2020, 6, 28),
            realtime_end=date(2020, 6, 28),
            date=date(2020, 1, 1),
            value=Decimal(value),
        )
    ]


def test_same_information_set_is_reused_across_evaluation_eods(
    postgres_session: Session,
) -> None:
    registry, materialization, generation = _generation(postgres_session)
    stored = persist_information_set(
        postgres_session,
        indicator_id="US_REAL_GDP",
        provider_series_id="GDPC1",
        source_information_set_date=date(2020, 6, 28),
        unit="chained_dollars",
        frequency="quarterly",
        observations=_observations("100.0"),
        raw_payload_hash="a" * 64,
        retrieved_at=datetime(2026, 8, 31, tzinfo=UTC),
        generation=generation,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
    )
    first = get_effective_observations(
        postgres_session,
        "US_REAL_GDP",
        date(2020, 1, 1),
        date(2020, 1, 1),
        date(2020, 6, 28),
        registry=registry,
        materialization=materialization,
    )
    later = get_effective_observations(
        postgres_session,
        "US_REAL_GDP",
        date(2020, 1, 1),
        date(2020, 1, 1),
        date(2020, 7, 19),
        registry=registry,
        materialization=materialization,
    )
    assert first[0].information_set_hash == stored.information_set_hash
    assert later[0].information_set_hash == stored.information_set_hash
    assert first[0].source_information_set_date == date(2020, 6, 28)
    assert later[0].source_information_set_date == date(2020, 6, 28)
    assert first[0].evaluation_result_hash != later[0].evaluation_result_hash
    assert first[0].vintage_as_of == first[0].evaluation_as_of
    assert later[0].vintage_as_of == later[0].evaluation_as_of


def test_raw_retrieval_change_is_idempotent_but_semantic_correction_is_versioned(
    postgres_session: Session,
) -> None:
    registry, materialization, generation = _generation(postgres_session)
    first = persist_information_set(
        postgres_session,
        indicator_id="US_REAL_GDP",
        provider_series_id="GDPC1",
        source_information_set_date=date(2020, 6, 28),
        unit="chained_dollars",
        frequency="quarterly",
        observations=_observations("100"),
        raw_payload_hash="a" * 64,
        retrieved_at=datetime(2026, 8, 30, tzinfo=UTC),
        generation=generation,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
    )
    same = persist_information_set(
        postgres_session,
        indicator_id="US_REAL_GDP",
        provider_series_id="GDPC1",
        source_information_set_date=date(2020, 6, 28),
        unit="chained_dollars",
        frequency="quarterly",
        observations=_observations("100.000"),
        raw_payload_hash="b" * 64,
        retrieved_at=datetime(2026, 8, 31, tzinfo=UTC),
        generation=generation,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
    )
    corrected = persist_information_set(
        postgres_session,
        indicator_id="US_REAL_GDP",
        provider_series_id="GDPC1",
        source_information_set_date=date(2020, 6, 28),
        unit="chained_dollars",
        frequency="quarterly",
        observations=_observations("101"),
        raw_payload_hash="c" * 64,
        retrieved_at=datetime(2026, 9, 1, tzinfo=UTC),
        generation=generation,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
    )
    assert same.id == first.id
    assert corrected.id != first.id
    assert corrected.information_set_hash != first.information_set_hash
    assert first.status == "SUPERSEDED"
    assert first.superseded_by_id == corrected.id
    assert len(postgres_session.scalars(select(PitInformationSet)).all()) == 2
    rows = postgres_session.scalars(
        select(ObservationVintage).where(
            ObservationVintage.indicator_id == "US_REAL_GDP",
            ObservationVintage.pit_information_set_id.is_not(None),
        )
    ).all()
    assert sorted(item.current_value for item in rows) == [Decimal("100"), Decimal("101")]


def test_persisted_semantic_hash_mismatch_fails_closed(postgres_session: Session) -> None:
    registry, materialization, generation = _generation(postgres_session)
    stored = persist_information_set(
        postgres_session,
        indicator_id="US_REAL_GDP",
        provider_series_id="GDPC1",
        source_information_set_date=date(2020, 6, 28),
        unit="chained_dollars",
        frequency="quarterly",
        observations=_observations("100"),
        raw_payload_hash="a" * 64,
        retrieved_at=datetime(2026, 8, 31, tzinfo=UTC),
        generation=generation,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
    )
    row = postgres_session.scalar(
        select(ObservationVintage).where(ObservationVintage.pit_information_set_id == stored.id)
    )
    assert row is not None
    row.current_value = Decimal("999")
    postgres_session.flush()
    with pytest.raises(PITInformationSetHashMismatch):
        get_effective_observations(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 1, 1),
            date(2020, 7, 19),
            registry=registry,
            materialization=materialization,
        )
