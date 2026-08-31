"""PostgreSQL integration tests for canonical current and arbitrary-date PIT features."""

import hashlib
import inspect
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_sources.fred.schemas import FredObservation
from app.database.models import (
    DataSourceRequest,
    IndicatorRegistry,
    ObservationVintage,
    RawObservation,
)
from app.features.schemas import FeatureStatus, QualityFlag
from app.features.service import (
    FeatureEngine,
    compute_feature,
    compute_feature_set,
    compute_indicator_features,
)
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.source_registry.config import load_source_registry
from app.source_registry.information_sets import persist_information_set
from app.source_registry.materialization import load_pit_materialization_catalog
from app.source_registry.pit_index import ChangeIndexDiscovery, persist_change_index_generation

pytestmark = pytest.mark.integration


def _seed(session: Session) -> None:
    registry = load_source_registry()
    seed_indicator_catalog(session, load_indicator_catalog(), registry)


def _request(session: Session, source: str, when: datetime) -> None:
    session.add(
        DataSourceRequest(
            source=source,
            endpoint="offline-feature-fixture",
            series_id=None,
            request_parameters_json={},
            started_at=when,
            finished_at=when,
            success=True,
            attempt_count=1,
            from_cache=False,
        )
    )


def _current(
    session: Session,
    indicator_id: str,
    observation_date: date,
    value: str,
    *,
    source: str = "fred",
) -> None:
    session.add(
        RawObservation(
            indicator_id=indicator_id,
            observation_date=observation_date,
            value=Decimal(value),
            source=source,
            retrieved_at=datetime(2026, 8, 26, tzinfo=UTC),
            raw_payload_hash=(indicator_id + observation_date.isoformat())
            .encode()
            .hex()[:64]
            .ljust(64, "0"),
            source_details_json={"release_date": observation_date.isoformat()},
        )
    )


def _pit_snapshot(
    session: Session,
    indicator_id: str,
    as_of: date,
    values: list[tuple[date, str]],
) -> None:
    registry = load_source_registry()
    materialization = load_pit_materialization_catalog(source_registry=registry)
    indicator = session.scalar(
        select(IndicatorRegistry).where(IndicatorRegistry.indicator_id == indicator_id)
    )
    assert indicator is not None and indicator.fred_series_id is not None
    generation_hash = hashlib.sha256(f"fixture-index:{indicator_id}:{as_of}".encode()).hexdigest()
    generation = persist_change_index_generation(
        session,
        indicator_id=indicator_id,
        provider_series_id=indicator.fred_series_id,
        discovery=ChangeIndexDiscovery(
            coverage_start=as_of,
            coverage_end=as_of,
            complete_through=as_of,
            change_dates=(as_of,),
            provider_count=1,
            pages=(),
            generation_hash=generation_hash,
        ),
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
        discovered_at=datetime(2026, 8, 26, tzinfo=UTC),
    )
    policy = registry.by_indicator()[indicator_id]
    observations = [
        FredObservation(
            realtime_start=as_of,
            realtime_end=as_of,
            date=observation_date,
            value=Decimal(value),
        )
        for observation_date, value in values
    ]
    persist_information_set(
        session,
        indicator_id=indicator_id,
        provider_series_id=indicator.fred_series_id,
        source_information_set_date=as_of,
        unit=policy.canonical.unit,
        frequency=policy.canonical.frequency,
        observations=observations,
        raw_payload_hash=hashlib.sha256(
            f"fixture-payload:{indicator_id}:{as_of}:{values}".encode()
        ).hexdigest(),
        retrieved_at=datetime(2026, 8, 26, tzinfo=UTC),
        generation=generation,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
    )


def _monthly_values(final: str = "103") -> list[tuple[date, str]]:
    from dateutil.relativedelta import relativedelta

    start = date(2023, 4, 1)
    return [
        (start + relativedelta(months=offset), str(100 + Decimal(offset) / Decimal(4)))
        for offset in range(12)
    ] + [(date(2024, 4, 1), final)]


@pytest.mark.asyncio
async def test_public_current_feature_reads_only_canonical_resolver(
    postgres_session: Session,
) -> None:
    _seed(postgres_session)
    _current(postgres_session, "US_SAVING_RATE", date(2026, 7, 1), "4.7")
    _request(postgres_session, "fred", datetime(2026, 8, 26, tzinfo=UTC))
    postgres_session.flush()
    results = await compute_feature(
        postgres_session,
        "US_SAVING_RATE__LEVEL",
        date(2026, 7, 1),
        date(2026, 7, 1),
    )
    assert len(results) == 1
    assert results[0].feature_status is FeatureStatus.OK
    assert results[0].value == Decimal("4.7")
    assert results[0].input_sources == ["fred"]
    assert results[0].as_of is None


@pytest.mark.asyncio
async def test_arbitrary_non_allowlisted_weekend_pit_feature_uses_semantic_information_set(
    postgres_session: Session,
) -> None:
    _seed(postgres_session)
    as_of = date(2024, 5, 5)
    registry = load_source_registry()
    assert as_of not in registry.by_indicator()["US_CPI"].vintage.validated_as_of_dates
    _pit_snapshot(postgres_session, "US_CPI", as_of, _monthly_values())
    postgres_session.flush()
    results = await FeatureEngine(postgres_session).compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED",
        date(2024, 4, 1),
        date(2024, 4, 1),
        as_of,
    )
    assert len(results) == 1
    result = results[0]
    assert result.feature_status is FeatureStatus.OK
    assert result.value is not None
    assert result.is_point_in_time_safe
    assert result.input_vintage_dates == [as_of] * 4
    assert result.registry_hash == registry.registry_hash
    assert max(result.input_observation_dates) <= as_of


@pytest.mark.asyncio
async def test_revised_pit_snapshot_changes_feature_without_overwriting_current(
    postgres_session: Session,
) -> None:
    _seed(postgres_session)
    before_as_of = date(2024, 5, 5)
    after_as_of = date(2024, 6, 2)
    _pit_snapshot(postgres_session, "US_CPI", before_as_of, _monthly_values("103"))
    _pit_snapshot(postgres_session, "US_CPI", after_as_of, _monthly_values("104"))
    _current(postgres_session, "US_CPI", date(2024, 4, 1), "105", source="bls")
    postgres_session.flush()
    engine = FeatureEngine(postgres_session)
    before = await engine.compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED",
        date(2024, 4, 1),
        date(2024, 4, 1),
        before_as_of,
    )
    after = await engine.compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED",
        date(2024, 4, 1),
        date(2024, 4, 1),
        after_as_of,
    )
    current = postgres_session.scalar(
        select(RawObservation).where(
            RawObservation.indicator_id == "US_CPI",
            RawObservation.source == "bls",
            RawObservation.observation_date == date(2024, 4, 1),
        )
    )
    assert before[0].value != after[0].value
    assert QualityFlag.REVISION_CHANGED_RESULT in after[0].quality_flags
    assert current is not None and current.value == Decimal("105")


@pytest.mark.asyncio
async def test_indicator_and_feature_set_public_apis_do_not_duplicate_persistence(
    postgres_session: Session,
) -> None:
    _seed(postgres_session)
    as_of = date(2024, 5, 5)
    _pit_snapshot(postgres_session, "US_CPI", as_of, _monthly_values())
    postgres_session.flush()
    before = postgres_session.scalar(
        select(func.count())
        .select_from(ObservationVintage)
        .where(ObservationVintage.indicator_id == "US_CPI")
    )
    indicator_results = await compute_indicator_features(
        postgres_session, "US_CPI", date(2024, 4, 1), date(2024, 4, 1), as_of
    )
    set_results = await compute_feature_set(
        postgres_session,
        [
            "US_CPI__INFLATION_3M_ANNUALIZED",
            "US_CPI__INFLATION_YOY",
        ],
        date(2024, 4, 1),
        date(2024, 4, 1),
        as_of,
    )
    after = postgres_session.scalar(
        select(func.count())
        .select_from(ObservationVintage)
        .where(ObservationVintage.indicator_id == "US_CPI")
    )
    assert len(indicator_results) == 4
    assert len(set_results) == 2
    assert before == after == 13


def test_public_domain_api_does_not_accept_provider_source_or_formula() -> None:
    for function in (compute_feature, compute_indicator_features, compute_feature_set):
        parameters = inspect.signature(function).parameters
        assert "provider" not in parameters
        assert "source" not in parameters
        assert "formula" not in parameters


def test_feature_engine_does_not_write_legacy_derived_features(
    postgres_session: Session,
) -> None:
    from app.database.models import DerivedFeature

    count = postgres_session.scalar(select(func.count()).select_from(DerivedFeature))
    assert count == 0
