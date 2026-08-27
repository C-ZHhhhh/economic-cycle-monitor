"""DLCR-002 integration coverage for orthogonal current and PIT health."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_request_session
from app.data_quality.checks import pit_availability_issue, source_unavailable_issue
from app.data_quality.repository import list_quality_events, persist_quality_issues
from app.data_quality.scope import (
    AUDIT_PIT_STATUS,
    AUDIT_QUALITY_SCOPE,
    AUDIT_RUNTIME_BLOCKING,
    AUDIT_RUNTIME_DOMAIN,
    PITAvailabilityStatus,
    QualityScope,
    RuntimeDomain,
    SafeErrorCategory,
    quality_event_scope,
    request_runtime_domain,
)
from app.data_quality.service import quality_summary_for_indicator
from app.database.models import DataQualityEvent, DataSourceRequest, SyncRun
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.main import create_app
from app.settings import Settings
from app.source_registry.health import (
    historical_audit_event_is_current,
    provider_health,
)
from app.source_registry.schemas import RuntimeHealth

pytestmark = pytest.mark.integration


def _seed(session: Session) -> None:
    seed_indicator_catalog(session, load_indicator_catalog())


def _run(
    session: Session, operation: str, *, parameters: dict[str, object] | None = None
) -> SyncRun:
    run = SyncRun(
        run_id=uuid4(),
        operation=operation,
        status="failed",
        started_at=datetime(2026, 8, 27, tzinfo=UTC),
        parameters_json=parameters or {},
        indicator_count=1,
    )
    session.add(run)
    session.flush()
    return run


def _request(
    session: Session,
    when: datetime,
    *,
    domain: RuntimeDomain | None,
    success: bool,
    runtime_blocking: bool = True,
    run: SyncRun | None = None,
    parameters: dict[str, object] | None = None,
) -> DataSourceRequest:
    audit: dict[str, object] = dict(parameters or {})
    if domain is not None:
        audit.update(
            {
                AUDIT_QUALITY_SCOPE: (
                    QualityScope.PIT_AVAILABILITY.value
                    if domain is RuntimeDomain.ALFRED_VINTAGE
                    else QualityScope.CURRENT_RUNTIME.value
                ),
                AUDIT_RUNTIME_DOMAIN: domain.value,
                AUDIT_RUNTIME_BLOCKING: runtime_blocking,
            }
        )
    request = DataSourceRequest(
        sync_run_id=run.run_id if run is not None else None,
        source="fred",
        endpoint="series/observations",
        series_id="GDPC1",
        request_parameters_json=audit,
        started_at=when,
        finished_at=when,
        success=success,
        attempt_count=1,
        error_type=None if success else "FredRequestError",
        error_message=None if success else "sanitized failure",
        from_cache=False,
    )
    session.add(request)
    session.flush()
    return request


def _event(
    session: Session,
    *,
    details: dict[str, object],
    run: SyncRun | None = None,
    fingerprint: str,
    resolved: bool = False,
) -> DataQualityEvent:
    event = DataQualityEvent(
        run_id=run.run_id if run is not None else None,
        indicator_id="US_REAL_GDP",
        event_type="source_unavailable",
        severity="error",
        message="sanitized failure",
        details_json=details,
        event_fingerprint=fingerprint,
        detected_at=datetime(2026, 8, 27, tzinfo=UTC),
        resolved_at=datetime(2026, 8, 28, tzinfo=UTC) if resolved else None,
        resolution_notes="superseded" if resolved else None,
    )
    session.add(event)
    session.flush()
    return event


@pytest.mark.parametrize(
    ("operation", "parameters", "explicit", "expected"),
    [
        ("sync_fred", {}, RuntimeDomain.ALFRED_VINTAGE, RuntimeDomain.ALFRED_VINTAGE),
        ("fetch_fred_vintage", {}, None, RuntimeDomain.ALFRED_VINTAGE),
        (
            "sync_fred",
            {"realtime_start": "2020-01-01", "realtime_end": "2020-01-01"},
            None,
            RuntimeDomain.ALFRED_VINTAGE,
        ),
        ("sync_fred", {}, None, RuntimeDomain.FRED_CURRENT),
    ],
)
def test_request_domain_prefers_explicit_then_reliable_legacy_evidence(
    postgres_session: Session,
    operation: str,
    parameters: dict[str, object],
    explicit: RuntimeDomain | None,
    expected: RuntimeDomain,
) -> None:
    request = _request(
        postgres_session,
        datetime(2026, 8, 27, tzinfo=UTC),
        domain=explicit,
        success=False,
        parameters=parameters,
    )
    assert request_runtime_domain(request, operation) is expected


def test_legacy_event_classification_is_reliable_conservative_and_immutable(
    postgres_session: Session,
) -> None:
    _seed(postgres_session)
    run = _run(postgres_session, "fetch_fred_vintage")
    linked = _event(postgres_session, details={}, run=run, fingerprint="a" * 64)
    realtime_run = _run(postgres_session, "legacy_research")
    _request(
        postgres_session,
        datetime(2026, 8, 27, 1, tzinfo=UTC),
        domain=None,
        success=False,
        run=realtime_run,
        parameters={"realtime_start": "2010-01-01", "realtime_end": "2010-01-01"},
    )
    realtime = _event(postgres_session, details={}, run=realtime_run, fingerprint="b" * 64)
    parameter_run = _run(
        postgres_session,
        "legacy_research",
        parameters={"as_of": "2010-01-01"},
    )
    parameter_linked = _event(
        postgres_session,
        details={},
        run=parameter_run,
        fingerprint="f" * 64,
    )
    ambiguous = _event(postgres_session, details={}, fingerprint="c" * 64)
    original = (linked.event_type, linked.detected_at, linked.message, linked.details_json.copy())

    assert quality_event_scope(postgres_session, linked) is QualityScope.PIT_AVAILABILITY
    assert quality_event_scope(postgres_session, realtime) is QualityScope.PIT_AVAILABILITY
    assert quality_event_scope(postgres_session, parameter_linked) is QualityScope.PIT_AVAILABILITY
    assert quality_event_scope(postgres_session, ambiguous) is QualityScope.CURRENT_RUNTIME
    assert (linked.event_type, linked.detected_at, linked.message, linked.details_json) == original


def test_explicit_scope_precedes_legacy_and_resolution_is_only_lifecycle(
    postgres_session: Session,
) -> None:
    _seed(postgres_session)
    vintage_run = _run(postgres_session, "fetch_fred_vintage")
    explicit_current = _event(
        postgres_session,
        details={"quality_scope": QualityScope.CURRENT_RUNTIME.value},
        run=vintage_run,
        fingerprint="d" * 64,
    )
    explicit_pit = _event(
        postgres_session,
        details={"quality_scope": QualityScope.PIT_AVAILABILITY.value},
        fingerprint="e" * 64,
        resolved=True,
    )

    assert quality_event_scope(postgres_session, explicit_current) is QualityScope.CURRENT_RUNTIME
    assert historical_audit_event_is_current(postgres_session, explicit_current)
    assert quality_event_scope(postgres_session, explicit_pit) is QualityScope.PIT_AVAILABILITY
    assert not historical_audit_event_is_current(postgres_session, explicit_pit)
    assert explicit_pit.resolution_notes == "superseded"


def test_fred_current_and_alfred_runtime_health_are_independent_and_recoverable(
    postgres_session: Session,
) -> None:
    base = datetime(2026, 8, 27, tzinfo=UTC)
    _request(
        postgres_session,
        base,
        domain=RuntimeDomain.FRED_CURRENT,
        success=True,
    )
    failure = _request(
        postgres_session,
        base + timedelta(minutes=1),
        domain=RuntimeDomain.ALFRED_VINTAGE,
        success=False,
    )

    assert provider_health(postgres_session, "fred").runtime_health is RuntimeHealth.HEALTHY
    assert (
        provider_health(
            postgres_session, "fred", runtime_domain=RuntimeDomain.ALFRED_VINTAGE
        ).runtime_health
        is RuntimeHealth.UNAVAILABLE
    )

    _request(
        postgres_session,
        base + timedelta(minutes=2),
        domain=RuntimeDomain.ALFRED_VINTAGE,
        success=True,
        runtime_blocking=False,
    )
    assert (
        provider_health(
            postgres_session, "fred", runtime_domain=RuntimeDomain.ALFRED_VINTAGE
        ).runtime_health
        is RuntimeHealth.HEALTHY
    )
    assert postgres_session.get(DataSourceRequest, failure.id) is failure

    _request(
        postgres_session,
        base + timedelta(minutes=3),
        domain=RuntimeDomain.FRED_CURRENT,
        success=False,
    )
    assert provider_health(postgres_session, "fred").runtime_health is RuntimeHealth.DEGRADED
    assert (
        provider_health(
            postgres_session, "fred", runtime_domain=RuntimeDomain.ALFRED_VINTAGE
        ).runtime_health
        is RuntimeHealth.HEALTHY
    )


@pytest.mark.parametrize(
    "status",
    [
        PITAvailabilityStatus.OUTSIDE_COVERAGE,
        PITAvailabilityStatus.NO_VINTAGE,
        PITAvailabilityStatus.EMPTY_INFORMATION_SET,
    ],
)
def test_expected_per_date_unavailability_does_not_mark_alfred_outage(
    postgres_session: Session,
    status: PITAvailabilityStatus,
) -> None:
    request = _request(
        postgres_session,
        datetime(2026, 8, 27, tzinfo=UTC),
        domain=RuntimeDomain.ALFRED_VINTAGE,
        success=False,
        runtime_blocking=False,
    )
    request.request_parameters_json[AUDIT_PIT_STATUS] = status.value
    postgres_session.flush()

    snapshot = provider_health(
        postgres_session,
        "fred",
        runtime_domain=RuntimeDomain.ALFRED_VINTAGE,
    )
    assert snapshot.runtime_health is RuntimeHealth.HEALTHY
    assert snapshot.reasons == ["LATEST_REQUEST_IS_EXPECTED_PIT_UNAVAILABILITY"]


def test_quality_summary_repository_and_api_default_to_current_scope(
    postgres_session: Session,
    settings: Settings,
) -> None:
    _seed(postgres_session)
    persist_quality_issues(
        postgres_session,
        [source_unavailable_issue("US_REAL_GDP", RuntimeError("current unavailable"))],
        run_id=None,
    )
    persist_quality_issues(
        postgres_session,
        [
            pit_availability_issue(
                "US_REAL_GDP",
                as_of=date(1940, 1, 1),
                status=PITAvailabilityStatus.OUTSIDE_COVERAGE,
                safe_error_category=SafeErrorCategory.COVERAGE,
                http_status=400,
                runtime_blocking=False,
            )
        ],
        run_id=None,
    )

    assert quality_summary_for_indicator(postgres_session, "US_REAL_GDP") == {"error": 1}
    assert quality_summary_for_indicator(
        postgres_session,
        "US_REAL_GDP",
        quality_scope=QualityScope.PIT_AVAILABILITY,
    ) == {"warning": 1}
    assert (
        len(
            list_quality_events(
                postgres_session,
                indicator_id="US_REAL_GDP",
                quality_scope=QualityScope.CURRENT_RUNTIME,
            )
        )
        == 1
    )

    application = create_app(settings)

    def session_override() -> Session:
        return postgres_session

    application.dependency_overrides[get_request_session] = session_override
    with TestClient(application) as client:
        current = client.get("/api/v1/data-quality", params={"indicator_id": "US_REAL_GDP"})
        pit = client.get(
            "/api/v1/data-quality",
            params={
                "indicator_id": "US_REAL_GDP",
                "quality_scope": QualityScope.PIT_AVAILABILITY.value,
            },
        )
    assert current.status_code == 200
    assert [item["event_type"] for item in current.json()] == ["source_unavailable"]
    assert pit.status_code == 200
    assert [item["event_type"] for item in pit.json()] == ["pit_availability_failure"]


def test_pit_event_persistence_is_idempotent_by_date_status_and_scope(
    postgres_session: Session,
) -> None:
    _seed(postgres_session)
    first = pit_availability_issue(
        "US_REAL_GDP",
        as_of=date(1940, 1, 1),
        status=PITAvailabilityStatus.OUTSIDE_COVERAGE,
        safe_error_category=SafeErrorCategory.COVERAGE,
        http_status=400,
        runtime_blocking=False,
    )
    second_date = pit_availability_issue(
        "US_REAL_GDP",
        as_of=date(1940, 1, 2),
        status=PITAvailabilityStatus.OUTSIDE_COVERAGE,
        safe_error_category=SafeErrorCategory.COVERAGE,
        http_status=400,
        runtime_blocking=False,
    )
    assert persist_quality_issues(postgres_session, [first], run_id=None) == 1
    assert persist_quality_issues(postgres_session, [first], run_id=None) == 0
    assert persist_quality_issues(postgres_session, [second_date], run_id=None) == 1
    assert (
        persist_quality_issues(
            postgres_session,
            [source_unavailable_issue("US_REAL_GDP", RuntimeError("current"))],
            run_id=None,
        )
        == 1
    )
    assert postgres_session.scalar(select(func.count()).select_from(DataQualityEvent)) == 3
