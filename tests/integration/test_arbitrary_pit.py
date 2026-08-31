"""Offline integration coverage for arbitrary-date ALFRED PIT resolution."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_quality.scope import (
    AUDIT_PIT_STATUS,
    AUDIT_QUALITY_SCOPE,
    AUDIT_RUNTIME_BLOCKING,
    AUDIT_RUNTIME_DOMAIN,
    PITAvailabilityStatus,
    QualityScope,
    RuntimeDomain,
)
from app.data_sources.fred.cache import FredFileCache
from app.data_sources.fred.client import FredClient
from app.data_sources.fred.raw_store import FredRawStore
from app.data_sources.fred.schemas import FredPayload, FredVintageDatePage
from app.database.models import (
    DataQualityEvent,
    DataSourceRequest,
    ObservationVintage,
    RawObservation,
    SyncRun,
)
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.settings import Settings
from app.source_registry.config import load_source_registry
from app.source_registry.health import provider_health
from app.source_registry.materialization import load_pit_materialization_catalog
from app.source_registry.pit import PITSnapshotFetchFailed, get_effective_observations_on_demand
from app.source_registry.pit_index import (
    persist_change_index_generation,
    validate_change_index_pages,
)
from app.source_registry.resolver import (
    PITChangeIndexUnavailable,
    PITCoverageUnverified,
    PITGranularityUnsupported,
    PITNoVintage,
    PITOutsideCoverage,
    get_effective_observations,
)
from app.source_registry.schemas import RuntimeHealth, SourceRegistryCatalog

pytestmark = pytest.mark.integration

TEST_KEY = "offline-pit-credential"
LIVE_GATE_FIXTURES = [
    ("US_REAL_GDP", "GDPC1", "quarterly"),
    ("US_CPI", "CPIAUCSL", "monthly"),
    ("US_NONFARM_EMPLOYMENT", "PAYEMS", "monthly"),
    ("US_INITIAL_CLAIMS", "ICSA", "weekly"),
    ("US_HOUSING_STARTS", "HOUST", "monthly"),
    ("US_INDUSTRIAL_PRODUCTION", "INDPRO", "monthly"),
]


def _seed(session: Session) -> SourceRegistryCatalog:
    registry = load_source_registry()
    seed_indicator_catalog(session, load_indicator_catalog(), registry)
    return registry


def _client(
    tmp_path: Path,
    handler: httpx.AsyncBaseTransport,
    *,
    cache_enabled: bool = True,
    auto_index: bool = True,
) -> FredClient:
    transport = _DefaultIndexTransport(handler) if auto_index else handler
    return FredClient(
        api_key=SecretStr(TEST_KEY),
        base_url="https://fred.invalid/fred",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        cache=FredFileCache(tmp_path / "cache", ttl_seconds=3600, enabled=cache_enabled),
        raw_store=FredRawStore(tmp_path / "raw"),
        transport=transport,
    )


class _DefaultIndexTransport(httpx.AsyncBaseTransport):
    """Supply one complete semantic change date before delegating observation calls."""

    def __init__(self, delegate: httpx.AsyncBaseTransport) -> None:
        self.delegate = delegate

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("series/vintagedates"):
            change_date = request.url.params["realtime_end"]
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "offset": int(request.url.params.get("offset", "0")),
                    "limit": int(request.url.params.get("limit", "10000")),
                    "vintage_dates": [change_date],
                },
            )
        return await self.delegate.handle_async_request(request)

    async def aclose(self) -> None:
        await self.delegate.aclose()


def _response(
    request: httpx.Request,
    *,
    observation_date: date = date(2020, 1, 1),
    value: str = "100.25",
) -> httpx.Response:
    as_of = request.url.params["realtime_start"]
    assert request.url.params["realtime_end"] == as_of
    assert request.url.params["api_key"] == TEST_KEY
    return httpx.Response(
        200,
        json={
            "observations": [
                {
                    "realtime_start": as_of,
                    "realtime_end": as_of,
                    "date": observation_date.isoformat(),
                    "value": value,
                }
            ]
        },
    )


def _persist_complete_index(
    session: Session,
    registry: SourceRegistryCatalog,
    *,
    change_dates: list[date],
    complete_through: date,
) -> None:
    """Persist complete synthetic pagination evidence for a fail-closed lookup test."""
    materialization = load_pit_materialization_catalog(source_registry=registry)
    now = datetime(2026, 8, 31, tzinfo=UTC)
    payload = FredPayload(
        endpoint="series/vintagedates",
        series_id="GDPC1",
        sanitized_parameters={},
        body={},
        started_at=now,
        finished_at=now,
        status_code=200,
        attempt_count=1,
        response_hash="f" * 64,
        response_path=None,
        from_cache=False,
    )
    discovery = validate_change_index_pages(
        [
            (
                FredVintageDatePage(
                    count=len(change_dates),
                    offset=0,
                    limit=10_000,
                    vintage_dates=change_dates,
                ),
                payload,
            )
        ],
        coverage_start=date(1776, 7, 4),
        coverage_end=complete_through,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
        indicator_id="US_REAL_GDP",
        provider_series_id="GDPC1",
    )
    persist_change_index_generation(
        session,
        indicator_id="US_REAL_GDP",
        provider_series_id="GDPC1",
        discovery=discovery,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
        discovered_at=now,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("indicator_id", "series_id", "frequency"), LIVE_GATE_FIXTURES)
async def test_arbitrary_non_allowlisted_dates_resolve_for_live_gate_families(
    postgres_session: Session,
    settings: Settings,
    tmp_path: Path,
    indicator_id: str,
    series_id: str,
    frequency: str,
) -> None:
    registry = _seed(postgres_session)
    arbitrary_date = date(2020, 7, 19)
    assert arbitrary_date not in registry.by_indicator()[indicator_id].vintage.validated_as_of_dates

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["series_id"] == series_id
        return _response(request)

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        rows = await get_effective_observations_on_demand(
            postgres_session,
            indicator_id,
            date(2020, 1, 1),
            date(2020, 1, 1),
            arbitrary_date,
            registry=registry,
            settings=settings,
            client=client,
        )

    assert len(rows) == 1
    assert rows[0].source_identity == series_id
    assert rows[0].frequency == frequency
    assert rows[0].is_point_in_time_safe


@pytest.mark.asyncio
async def test_legacy_exact_snapshot_is_not_a_production_fallback(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)
    as_of = date(2020, 6, 28)
    postgres_session.add(
        ObservationVintage(
            indicator_id="US_REAL_GDP",
            observation_date=date(2020, 1, 1),
            vintage_date=as_of,
            release_stage="as_known_on",
            revision_number=1,
            current_value=Decimal("19000"),
            source="fred",
            source_vintage_date=as_of,
            retrieved_at=datetime(2026, 8, 26, tzinfo=UTC),
            raw_payload_hash="a" * 64,
        )
    )
    postgres_session.flush()

    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        with pytest.raises(PITSnapshotFetchFailed):
            await get_effective_observations_on_demand(
                postgres_session,
                "US_REAL_GDP",
                date(2020, 1, 1),
                date(2020, 1, 1),
                as_of,
                registry=registry,
                settings=settings,
                client=client,
            )
    assert calls == 1
    legacy = postgres_session.scalar(
        select(ObservationVintage).where(ObservationVintage.release_stage == "as_known_on")
    )
    assert legacy is not None and legacy.current_value == Decimal("19000")


@pytest.mark.asyncio
async def test_local_miss_fetches_once_and_repeat_is_database_idempotent(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(request)

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        first = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 1, 1),
            date(2020, 7, 19),
            registry=registry,
            settings=settings,
            client=client,
        )
        second = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 1, 1),
            date(2020, 7, 19),
            registry=registry,
            settings=settings,
            client=client,
        )
    count = postgres_session.scalar(
        select(func.count())
        .select_from(ObservationVintage)
        .where(
            ObservationVintage.indicator_id == "US_REAL_GDP",
            ObservationVintage.vintage_date == date(2020, 7, 19),
        )
    )
    assert calls == 1
    assert count == 1
    assert first == second


@pytest.mark.asyncio
async def test_same_date_revision_row_does_not_masquerade_as_exact_snapshot(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)
    as_of = date(2020, 7, 19)
    postgres_session.add(
        ObservationVintage(
            indicator_id="US_REAL_GDP",
            observation_date=date(2020, 1, 1),
            vintage_date=as_of,
            release_stage="revision",
            revision_number=1,
            current_value=Decimal("99"),
            source="fred",
            source_vintage_date=as_of,
            raw_payload_hash="e" * 64,
        )
    )
    postgres_session.flush()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(request, value="100")

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        rows = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 1, 1),
            as_of,
            registry=registry,
            settings=settings,
            client=client,
        )
    assert calls == 1
    assert rows[0].value == Decimal("100")
    assert (
        postgres_session.scalar(
            select(func.count())
            .select_from(ObservationVintage)
            .where(
                ObservationVintage.indicator_id == "US_REAL_GDP",
                ObservationVintage.vintage_date == as_of,
            )
        )
        == 2
    )


@pytest.mark.asyncio
async def test_two_as_of_dates_are_isolated_and_deterministic(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        as_of = request.url.params["realtime_start"]
        calls.append(as_of)
        value = "100" if as_of == "2020-06-28" else "101"
        return _response(request, value=value)

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        before = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 1, 1),
            date(2020, 6, 28),
            registry=registry,
            settings=settings,
            client=client,
        )
        after = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 1, 1),
            date(2020, 7, 19),
            registry=registry,
            settings=settings,
            client=client,
        )
    assert calls == ["2020-06-28", "2020-07-19"]
    assert before[0].value == Decimal("100")
    assert after[0].value == Decimal("101")


@pytest.mark.asyncio
async def test_weekend_date_returns_sunday_eod_information_set(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["realtime_start"] == "2020-07-19"
        return _response(request)

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        rows = await get_effective_observations_on_demand(
            postgres_session,
            "US_CPI",
            date(2020, 1, 1),
            date(2020, 1, 1),
            date(2020, 7, 19),
            registry=registry,
            settings=settings,
            client=client,
        )
    assert rows[0].vintage_as_of == date(2020, 7, 19)
    stored = postgres_session.scalar(
        select(ObservationVintage).where(ObservationVintage.indicator_id == "US_CPI")
    )
    assert stored is not None
    assert stored.source_details_json["information_set_semantics"] == "END_OF_DAY"
    assert stored.available_at is not None
    assert stored.available_at.date() == date(2020, 7, 19)


@pytest.mark.asyncio
async def test_future_dated_observation_is_not_persisted_or_returned(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)

    def handler(request: httpx.Request) -> httpx.Response:
        as_of = request.url.params["realtime_start"]
        return httpx.Response(
            200,
            json={
                "observations": [
                    {
                        "realtime_start": as_of,
                        "realtime_end": as_of,
                        "date": "2020-01-01",
                        "value": "100",
                    },
                    {
                        "realtime_start": as_of,
                        "realtime_end": as_of,
                        "date": "2021-01-01",
                        "value": "999",
                    },
                ]
            },
        )

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        rows = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2021, 1, 1),
            date(2020, 7, 19),
            registry=registry,
            settings=settings,
            client=client,
        )
    assert [row.observation_date for row in rows] == [date(2020, 1, 1)]
    assert (
        postgres_session.scalar(
            select(ObservationVintage.id).where(
                ObservationVintage.observation_date == date(2021, 1, 1)
            )
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"observations": "bad"},
        {"observations": []},
        {
            "observations": [
                {
                    "realtime_start": "2020-07-19",
                    "realtime_end": "2020-07-19",
                    "date": "2020-01-01",
                    "value": ".",
                }
            ]
        },
    ],
)
async def test_malformed_or_empty_business_response_fails_closed(
    postgres_session: Session,
    settings: Settings,
    tmp_path: Path,
    body: dict[str, object],
) -> None:
    registry = _seed(postgres_session)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("series/vintagedates"):
            return httpx.Response(200, json={"vintage_dates": ["2010-01-01"]})
        return httpx.Response(200, json=body)

    async with _client(tmp_path, httpx.MockTransport(handler), cache_enabled=False) as client:
        with pytest.raises(PITSnapshotFetchFailed) as captured:
            await get_effective_observations_on_demand(
                postgres_session,
                "US_REAL_GDP",
                date(2020, 1, 1),
                date(2020, 1, 1),
                date(2020, 7, 19),
                registry=registry,
                settings=settings,
                client=client,
            )
    expected = (
        PITAvailabilityStatus.MALFORMED_RESPONSE
        if body.get("observations")
        not in (
            [],
            [
                {
                    "realtime_start": "2020-07-19",
                    "realtime_end": "2020-07-19",
                    "date": "2020-01-01",
                    "value": ".",
                }
            ],
        )
        else PITAvailabilityStatus.EMPTY_INFORMATION_SET
    )
    assert captured.value.availability_status is expected
    assert postgres_session.scalar(select(ObservationVintage.id)) is None
    event = postgres_session.scalar(
        select(DataQualityEvent).where(DataQualityEvent.event_type == "pit_availability_failure")
    )
    assert event is not None
    assert event.details_json["pit_availability_status"] == expected.value
    alfred = provider_health(
        postgres_session,
        "fred",
        runtime_domain=RuntimeDomain.ALFRED_VINTAGE,
    )
    assert alfred.runtime_health is (
        RuntimeHealth.HEALTHY
        if expected is PITAvailabilityStatus.EMPTY_INFORMATION_SET
        else RuntimeHealth.DEGRADED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 429, 500])
async def test_http_failures_never_fall_back_to_current_snapshot(
    postgres_session: Session,
    settings: Settings,
    tmp_path: Path,
    status_code: int,
) -> None:
    registry = _seed(postgres_session)
    postgres_session.add(
        RawObservation(
            indicator_id="US_REAL_GDP",
            observation_date=date(2020, 1, 1),
            value=Decimal("999"),
            source="fred",
            retrieved_at=datetime(2026, 8, 26, tzinfo=UTC),
            raw_payload_hash="b" * 64,
            source_details_json={},
        )
    )
    postgres_session.flush()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    async with _client(tmp_path, httpx.MockTransport(handler), cache_enabled=False) as client:
        with pytest.raises(PITSnapshotFetchFailed):
            await get_effective_observations_on_demand(
                postgres_session,
                "US_REAL_GDP",
                date(2020, 1, 1),
                date(2020, 1, 1),
                date(2020, 7, 19),
                registry=registry,
                settings=settings,
                client=client,
            )
    assert postgres_session.scalar(select(ObservationVintage.id)) is None
    request = postgres_session.scalar(
        select(DataSourceRequest).where(DataSourceRequest.endpoint == "series/observations")
    )
    assert request is not None
    assert request.request_parameters_json == {
        "realtime_start": "2020-07-19",
        "realtime_end": "2020-07-19",
        AUDIT_QUALITY_SCOPE: QualityScope.PIT_AVAILABILITY.value,
        AUDIT_RUNTIME_DOMAIN: RuntimeDomain.ALFRED_VINTAGE.value,
        AUDIT_PIT_STATUS: (
            PITAvailabilityStatus.AUTHENTICATION_FAILURE.value
            if status_code in {401, 403}
            else PITAvailabilityStatus.TEMPORARY_REQUEST_FAILURE.value
            if status_code in {429, 500}
            else PITAvailabilityStatus.UNKNOWN_FAILURE.value
        ),
        AUDIT_RUNTIME_BLOCKING: True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vintage_dates", "as_of", "expected"),
    [
        (["2010-01-01", "2010-02-01"], date(1940, 1, 1), PITAvailabilityStatus.OUTSIDE_COVERAGE),
        ([], date(1940, 1, 1), PITAvailabilityStatus.NO_VINTAGE),
    ],
)
async def test_complete_index_proves_expected_unavailability_without_observation_request(
    postgres_session: Session,
    settings: Settings,
    tmp_path: Path,
    vintage_dates: list[str],
    as_of: date,
    expected: PITAvailabilityStatus,
) -> None:
    registry = _seed(postgres_session)
    _persist_complete_index(
        postgres_session,
        registry,
        change_dates=[date.fromisoformat(item) for item in vintage_dates],
        complete_through=date(2020, 12, 31),
    )
    error = (
        PITOutsideCoverage if expected is PITAvailabilityStatus.OUTSIDE_COVERAGE else PITNoVintage
    )
    for _ in range(2):
        with pytest.raises(error):
            get_effective_observations(
                postgres_session,
                "US_REAL_GDP",
                date(1940, 1, 1),
                date(1940, 1, 1),
                as_of,
                registry=registry,
            )


@pytest.mark.asyncio
async def test_http_400_with_insufficient_coverage_evidence_remains_unknown(
    postgres_session: Session,
    settings: Settings,
    tmp_path: Path,
) -> None:
    registry = _seed(postgres_session)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("series/vintagedates"):
            return httpx.Response(200, json={"vintage_dates": ["2010-01-01"]})
        return httpx.Response(400)

    async with _client(tmp_path, httpx.MockTransport(handler), cache_enabled=False) as client:
        with pytest.raises(PITSnapshotFetchFailed) as captured:
            await get_effective_observations_on_demand(
                postgres_session,
                "US_REAL_GDP",
                date(2020, 1, 1),
                date(2020, 1, 1),
                date(2020, 7, 19),
                registry=registry,
                settings=settings,
                client=client,
            )
    assert captured.value.availability_status is PITAvailabilityStatus.UNKNOWN_FAILURE
    event = postgres_session.scalar(
        select(DataQualityEvent).where(DataQualityEvent.event_type == "pit_availability_failure")
    )
    assert event is not None
    assert event.details_json["runtime_blocking"] is True


@pytest.mark.asyncio
async def test_pit_timeout_is_temporary_and_audit_is_url_and_credential_free(
    postgres_session: Session,
    settings: Settings,
    tmp_path: Path,
) -> None:
    registry = _seed(postgres_session)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("safe fixture", request=request)

    async with _client(tmp_path, httpx.MockTransport(handler), cache_enabled=False) as client:
        with pytest.raises(PITSnapshotFetchFailed) as captured:
            await get_effective_observations_on_demand(
                postgres_session,
                "US_REAL_GDP",
                date(2020, 1, 1),
                date(2020, 1, 1),
                date(2020, 7, 19),
                registry=registry,
                settings=settings,
                client=client,
            )
    assert captured.value.availability_status is PITAvailabilityStatus.TEMPORARY_REQUEST_FAILURE
    request = postgres_session.scalar(select(DataSourceRequest))
    run = postgres_session.scalar(select(SyncRun))
    event = postgres_session.scalar(select(DataQualityEvent))
    assert request is not None and run is not None and event is not None
    audit = json.dumps(
        {
            "endpoint": request.endpoint,
            "parameters": request.request_parameters_json,
            "error": request.error_message,
            "run_errors": run.error_summary_json,
            "event": event.details_json,
        }
    )
    assert TEST_KEY not in audit
    assert "fred.invalid" not in audit
    assert "api_key" not in audit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "backtest_safety"),
    [
        ("ALFRED_VINTAGE_AVAILABLE_BUT_LIMITED", "PIT_LIMITED"),
        ("CURRENT_SNAPSHOT_ONLY", "CURRENT_SNAPSHOT_ONLY"),
        ("NO_VINTAGE", "UNSAFE"),
        ("PIT_UNVERIFIED", "UNVERIFIED"),
    ],
)
async def test_non_safe_capabilities_fail_before_transport(
    postgres_session: Session,
    settings: Settings,
    tmp_path: Path,
    capability: str,
    backtest_safety: str,
) -> None:
    registry = _seed(postgres_session)
    policy = registry.by_indicator()["US_REAL_GDP"]
    unsafe = policy.model_copy(
        update={
            "vintage": policy.vintage.model_copy(
                update={"capability": capability, "backtest_safety": backtest_safety}
            )
        }
    )
    unsafe_registry = registry.model_copy(
        update={
            "policies": [
                unsafe if item.indicator_id == unsafe.indicator_id else item
                for item in registry.policies
            ]
        }
    )

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("unsafe capability must not call ALFRED")

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        with pytest.raises(PITCoverageUnverified, match="PIT_COVERAGE_UNVERIFIED") as captured:
            await get_effective_observations_on_demand(
                postgres_session,
                "US_REAL_GDP",
                date(2020, 1, 1),
                date(2020, 1, 1),
                date(2020, 7, 19),
                registry=unsafe_registry,
                settings=settings,
                client=client,
            )
    assert captured.value.availability_status is PITAvailabilityStatus.UNSAFE


@pytest.mark.asyncio
async def test_timestamp_is_rejected_without_truncation_or_transport(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("timestamp rejection must happen before ALFRED")

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        with pytest.raises(PITGranularityUnsupported, match="PIT_GRANULARITY_UNSUPPORTED"):
            await get_effective_observations_on_demand(
                postgres_session,
                "US_REAL_GDP",
                date(2020, 1, 1),
                date(2020, 1, 1),
                datetime(2020, 7, 19, 12, 0, tzinfo=UTC),
                registry=registry,
                settings=settings,
                client=client,
            )


@pytest.mark.asyncio
async def test_canonical_provenance_includes_frozen_registry_identity(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(request)

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        rows = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 1, 1),
            date(2020, 7, 19),
            registry=registry,
            settings=settings,
            client=client,
        )
    row = rows[0]
    assert row.registry_hash == registry.registry_hash
    assert row.source_policy_version == "stage2b5-v3"
    assert row.vintage_source == "ALFRED_VINTAGE"
    assert row.vintage_as_of == date(2020, 7, 19)
    assert row.resolution_reason == "ALFRED_SEMANTIC_INFORMATION_SET"
    assert row.source_used == "fred"
    assert not row.is_fallback


@pytest.mark.asyncio
async def test_request_audit_and_public_errors_are_secret_free(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(request)

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 1, 1),
            date(2020, 7, 19),
            registry=registry,
            settings=settings,
            client=client,
        )
    request = postgres_session.scalar(
        select(DataSourceRequest).where(DataSourceRequest.endpoint == "series/observations")
    )
    run = postgres_session.scalar(select(SyncRun))
    assert request is not None and run is not None
    evidence = json.dumps(
        {
            "request": request.request_parameters_json,
            "errors": run.error_summary_json,
            "path": request.response_path,
        }
    )
    assert TEST_KEY not in evidence
    assert "api_key" not in request.request_parameters_json
    assert request.request_parameters_json["realtime_start"] == "2020-07-19"
    assert request.request_parameters_json[AUDIT_QUALITY_SCOPE] == QualityScope.PIT_AVAILABILITY
    assert request.request_parameters_json[AUDIT_RUNTIME_DOMAIN] == RuntimeDomain.ALFRED_VINTAGE
    assert request.request_parameters_json[AUDIT_PIT_STATUS] == PITAvailabilityStatus.AVAILABLE


@pytest.mark.asyncio
async def test_missing_values_are_not_coerced_to_zero(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)

    def handler(request: httpx.Request) -> httpx.Response:
        as_of = request.url.params["realtime_start"]
        return httpx.Response(
            200,
            json={
                "observations": [
                    {
                        "realtime_start": as_of,
                        "realtime_end": as_of,
                        "date": "2019-10-01",
                        "value": ".",
                    },
                    {
                        "realtime_start": as_of,
                        "realtime_end": as_of,
                        "date": "2020-01-01",
                        "value": "100",
                    },
                ]
            },
        )

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        rows = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2019, 10, 1),
            date(2020, 1, 1),
            date(2020, 7, 19),
            registry=registry,
            settings=settings,
            client=client,
        )
    assert [(row.observation_date, row.value) for row in rows] == [
        (date(2020, 1, 1), Decimal("100"))
    ]


@pytest.mark.asyncio
async def test_existing_snapshot_can_legitimately_return_empty_requested_range(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)
    as_of = date(2020, 7, 19)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(request, value="100")

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        rows = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2019, 1, 1),
            date(2019, 12, 31),
            as_of,
            registry=registry,
            settings=settings,
            client=client,
        )
    assert rows == []
    assert calls == 1

    def reject(_: httpx.Request) -> httpx.Response:
        raise AssertionError("semantic information-set hit must remain local")

    async with _client(tmp_path, httpx.MockTransport(reject)) as client:
        repeated = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2019, 1, 1),
            date(2019, 12, 31),
            as_of,
            registry=registry,
            settings=settings,
            client=client,
        )
    assert repeated == []


def test_sync_reader_requires_a_complete_change_index(
    postgres_session: Session,
) -> None:
    registry = _seed(postgres_session)
    arbitrary = date(2020, 7, 19)
    with pytest.raises(PITChangeIndexUnavailable):
        get_effective_observations(
            postgres_session,
            "US_CPI",
            date(2020, 1, 1),
            date(2020, 1, 1),
            arbitrary,
            registry=registry,
        )


@pytest.mark.asyncio
async def test_start_after_end_fails_before_transport(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid range must not call ALFRED")

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="start must not be after end"):
            await get_effective_observations_on_demand(
                postgres_session,
                "US_REAL_GDP",
                date(2020, 2, 1),
                date(2020, 1, 1),
                date(2020, 7, 19),
                registry=registry,
                settings=settings,
                client=client,
            )


@pytest.mark.asyncio
async def test_pit_rows_are_returned_in_deterministic_observation_order(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)

    def handler(request: httpx.Request) -> httpx.Response:
        as_of = request.url.params["realtime_start"]
        return httpx.Response(
            200,
            json={
                "observations": [
                    {
                        "realtime_start": as_of,
                        "realtime_end": as_of,
                        "date": "2020-04-01",
                        "value": "101",
                    },
                    {
                        "realtime_start": as_of,
                        "realtime_end": as_of,
                        "date": "2020-01-01",
                        "value": "100",
                    },
                ]
            },
        )

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        rows = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 4, 1),
            date(2020, 7, 19),
            registry=registry,
            settings=settings,
            client=client,
        )
    assert [row.observation_date for row in rows] == [date(2020, 1, 1), date(2020, 4, 1)]


@pytest.mark.asyncio
async def test_revision_dates_change_pit_value_without_overwriting_current_data(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    registry = _seed(postgres_session)
    postgres_session.add(
        RawObservation(
            indicator_id="US_REAL_GDP",
            observation_date=date(2020, 1, 1),
            value=Decimal("102"),
            source="bea",
            retrieved_at=datetime(2026, 8, 26, tzinfo=UTC),
            raw_payload_hash="d" * 64,
            source_details_json={},
        )
    )
    postgres_session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        value = "100" if request.url.params["realtime_start"] == "2020-06-28" else "101"
        return _response(request, value=value)

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        before = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 1, 1),
            date(2020, 6, 28),
            registry=registry,
            settings=settings,
            client=client,
        )
        after = await get_effective_observations_on_demand(
            postgres_session,
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 1, 1),
            date(2020, 7, 19),
            registry=registry,
            settings=settings,
            client=client,
        )
    current = postgres_session.scalar(
        select(RawObservation).where(
            RawObservation.indicator_id == "US_REAL_GDP",
            RawObservation.source == "bea",
        )
    )
    assert before[0].value == Decimal("100")
    assert after[0].value == Decimal("101")
    assert current is not None and current.value == Decimal("102")
