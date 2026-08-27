"""PostgreSQL integration tests for Stage 3A-to-Stage 3B signal flow."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import (
    DataSourceRequest,
    DerivedFeature,
    ObservationVintage,
    RawObservation,
)
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.signals.schemas import SignalQualityFlag, SignalStatus
from app.signals.service import SignalEngine, compute_feature_signals
from app.source_registry.config import load_source_registry

pytestmark = pytest.mark.integration

FEATURE_ID = "US_SAVING_RATE__LEVEL"


def _seed(session: Session) -> None:
    registry = load_source_registry()
    seed_indicator_catalog(session, load_indicator_catalog(), registry)


def _request(session: Session, when: datetime) -> None:
    session.add(
        DataSourceRequest(
            source="fred",
            endpoint="offline-signal-fixture",
            series_id=None,
            request_parameters_json={},
            started_at=when,
            finished_at=when,
            success=True,
            attempt_count=1,
            from_cache=False,
        )
    )


def _current_monthly(session: Session, start: date, count: int) -> list[date]:
    dates: list[date] = []
    for index in range(count):
        observation_date = start + relativedelta(months=index)
        dates.append(observation_date)
        session.add(
            RawObservation(
                indicator_id="US_SAVING_RATE",
                observation_date=observation_date,
                value=Decimal(index),
                source="fred",
                retrieved_at=datetime(2026, 8, 27, tzinfo=UTC),
                raw_payload_hash=(f"current:{observation_date}").encode().hex()[:64].ljust(64, "0"),
                source_details_json={"release_date": observation_date.isoformat()},
            )
        )
    return dates


def _pit_monthly(
    session: Session,
    *,
    as_of: date,
    start: date,
    count: int,
    revised_history_value: Decimal | None = None,
) -> list[date]:
    dates: list[date] = []
    for index in range(count):
        observation_date = start + relativedelta(months=index)
        dates.append(observation_date)
        value = Decimal(index)
        if revised_history_value is not None and index == count - 2:
            value = revised_history_value
        session.add(
            ObservationVintage(
                indicator_id="US_SAVING_RATE",
                observation_date=observation_date,
                vintage_date=as_of,
                release_date=observation_date,
                available_at=datetime(as_of.year, as_of.month, as_of.day, 23, 59, tzinfo=UTC),
                release_stage="as_known_on",
                revision_number=1,
                current_value=value,
                source="fred",
                source_vintage_date=as_of,
                retrieved_at=datetime(2026, 8, 27, tzinfo=UTC),
                raw_payload_hash=(f"pit:{as_of}:{observation_date}:{value}")
                .encode()
                .hex()[:64]
                .ljust(64, "0"),
                source_details_json={"information_set_semantics": "END_OF_DAY"},
            )
        )
    return dates


@pytest.mark.asyncio
async def test_current_signal_consumes_real_stage3a_feature_engine(
    postgres_session: Session,
) -> None:
    _seed(postgres_session)
    dates = _current_monthly(postgres_session, date(2018, 1, 1), 61)
    _request(postgres_session, datetime(2026, 8, 27, tzinfo=UTC))
    postgres_session.flush()
    result = (await compute_feature_signals(postgres_session, FEATURE_ID, dates[-1], dates[-1]))[0]
    assert result.signal_status is SignalStatus.OK
    assert result.history_count == 60
    assert result.center == Decimal("29.5")
    assert result.history_end is not None
    assert result.history_end < result.feature_observation_date
    assert not result.distribution_includes_current
    assert result.raw_feature_value == Decimal(60)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("as_of", "start"),
    [
        (date(2008, 10, 5), date(2003, 9, 1)),
        (date(2020, 5, 3), date(2015, 4, 1)),
        (date(2022, 7, 3), date(2017, 6, 1)),
    ],
)
async def test_crisis_episode_fixture_pit_uses_one_exact_information_set(
    postgres_session: Session,
    as_of: date,
    start: date,
) -> None:
    _seed(postgres_session)
    dates = _pit_monthly(postgres_session, as_of=as_of, start=start, count=61)
    postgres_session.flush()
    result = (
        await SignalEngine(postgres_session).compute_feature_signals(
            FEATURE_ID, dates[-1], dates[-1], as_of
        )
    )[0]
    assert result.signal_status is SignalStatus.OK
    assert result.as_of == as_of
    assert result.is_point_in_time_safe
    assert result.history_count == 60
    assert result.history_end is not None
    assert result.history_end < result.feature_observation_date <= as_of


@pytest.mark.asyncio
async def test_pit_revision_changes_raw_distribution_and_signal(
    postgres_session: Session,
) -> None:
    _seed(postgres_session)
    before_as_of = date(2020, 5, 3)
    after_as_of = date(2020, 6, 7)
    start = date(2015, 4, 1)
    dates = _pit_monthly(postgres_session, as_of=before_as_of, start=start, count=61)
    _pit_monthly(
        postgres_session,
        as_of=after_as_of,
        start=start,
        count=61,
        revised_history_value=Decimal(500),
    )
    postgres_session.flush()
    engine = SignalEngine(postgres_session)
    before = (await engine.compute_feature_signals(FEATURE_ID, dates[-1], dates[-1], before_as_of))[
        0
    ]
    after = (await engine.compute_feature_signals(FEATURE_ID, dates[-1], dates[-1], after_as_of))[0]
    assert before.center != after.center
    assert before.scale != after.scale
    assert before.normalized_value != after.normalized_value
    assert before.calculation_hash != after.calculation_hash
    assert SignalQualityFlag.REVISION_CHANGED_DISTRIBUTION in after.quality_flags


@pytest.mark.asyncio
async def test_signal_engine_never_writes_legacy_derived_features(
    postgres_session: Session,
) -> None:
    _seed(postgres_session)
    dates = _current_monthly(postgres_session, date(2018, 1, 1), 61)
    _request(postgres_session, datetime(2026, 8, 27, tzinfo=UTC))
    postgres_session.flush()
    before = postgres_session.scalar(select(func.count()).select_from(DerivedFeature))
    await compute_feature_signals(postgres_session, FEATURE_ID, dates[-1], dates[-1])
    after = postgres_session.scalar(select(func.count()).select_from(DerivedFeature))
    assert before == after == 0
