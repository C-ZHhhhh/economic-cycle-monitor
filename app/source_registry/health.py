"""Computed provider and indicator/source runtime health."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    DataQualityEvent,
    DataSourceRequest,
    IndicatorSourceMapping,
    RawObservation,
    SyncRun,
)
from app.source_registry.config import storage_source_name
from app.source_registry.schemas import (
    HealthSnapshot,
    MappingQuality,
    RuntimeHealth,
    SourceRegistryPolicy,
    SourceSystem,
)

INTEGRITY_EVENT_TYPES = {
    "source_value_conflict",
    "normalization_mismatch",
    "seasonal_adjustment_mismatch",
    "unexpected_source_identity",
}


def mapping_quality_from_status(status: str) -> MappingQuality:
    """Translate persisted lowercase mapping states into the public taxonomy."""
    return {
        "valid": MappingQuality.VALID,
        "valid_with_warning": MappingQuality.VALID_WITH_METADATA_WARNING,
        "pending_review": MappingQuality.PENDING_REVIEW,
        "invalid": MappingQuality.INVALID,
        "unavailable": MappingQuality.UNAVAILABLE,
    }[status]


def expected_latest_period(policy: SourceRegistryPolicy, *, today: date) -> date:
    """Compute the latest expected observation period from schedule lag policy."""
    if policy.freshness.release_calendar:
        timezone = ZoneInfo(policy.freshness.release_timezone or "UTC")
        evaluated_at = datetime.combine(today, time.max, tzinfo=timezone)
        eligible = [
            item
            for item in policy.freshness.release_calendar
            if item.scheduled_release_at <= evaluated_at
        ]
        return (eligible[-1] if eligible else policy.freshness.release_calendar[0]).period
    candidate = today - timedelta(days=policy.freshness.expected_lag_days)
    frequency = policy.canonical.frequency
    if frequency == "weekly":
        return candidate - timedelta(days=(candidate.weekday() - 5) % 7)
    if frequency == "monthly":
        return candidate.replace(day=1)
    quarter_month = ((candidate.month - 1) // 3) * 3 + 1
    return candidate.replace(month=quarter_month, day=1) - relativedelta(months=3)


def expected_period_due_date(policy: SourceRegistryPolicy, expected_period: date) -> date:
    """Return the approximate due date for an expected observation period."""
    return expected_period + timedelta(days=policy.freshness.expected_lag_days)


@dataclass(frozen=True)
class FreshnessExpectation:
    """Expected period and its official or approximate release boundary."""

    period: date
    scheduled_release_at: datetime
    grace_ends_at: datetime
    schedule_is_certain: bool


def freshness_expectation(
    policy: SourceRegistryPolicy,
    *,
    today: date | None = None,
    evaluated_at: datetime | None = None,
) -> FreshnessExpectation:
    """Resolve the expected period with official calendars taking precedence."""
    if evaluated_at is not None and evaluated_at.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    if policy.freshness.release_calendar:
        timezone = ZoneInfo(policy.freshness.release_timezone or "UTC")
        instant = (
            evaluated_at.astimezone(timezone)
            if evaluated_at is not None
            else datetime.combine(today, time.max, tzinfo=timezone)
            if today is not None
            else datetime.now(timezone)
        )
        entries = policy.freshness.release_calendar
        eligible = [item for item in entries if item.scheduled_release_at <= instant]
        entry = eligible[-1] if eligible else entries[0]
        scheduled = entry.scheduled_release_at.astimezone(timezone)
        return FreshnessExpectation(
            period=entry.period,
            scheduled_release_at=scheduled,
            grace_ends_at=scheduled + timedelta(days=policy.freshness.grace_days),
            schedule_is_certain=bool(eligible) and instant <= entries[-1].scheduled_release_at,
        )

    evaluation_date = today or (evaluated_at or datetime.now(UTC)).date()
    period = expected_latest_period(policy, today=evaluation_date)
    due_date = expected_period_due_date(policy, period)
    scheduled = datetime.combine(due_date, time.max, tzinfo=UTC)
    return FreshnessExpectation(
        period=period,
        scheduled_release_at=scheduled,
        grace_ends_at=scheduled + timedelta(days=policy.freshness.grace_days),
        schedule_is_certain=True,
    )


def _latest_request(
    session: Session, source: str, *, success: bool | None = None
) -> DataSourceRequest | None:
    statement = select(DataSourceRequest).where(DataSourceRequest.source == source)
    if success is not None:
        statement = statement.where(DataSourceRequest.success.is_(success))
    return session.scalar(statement.order_by(DataSourceRequest.started_at.desc()).limit(1))


def provider_health(session: Session, source: str) -> HealthSnapshot:
    """Compute provider-level availability without mutating registry policy."""
    latest = _latest_request(session, source)
    success = _latest_request(session, source, success=True)
    latest_run = session.scalar(
        select(SyncRun)
        .where(SyncRun.operation.contains(source))
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    )
    reasons: list[str] = []
    if latest is None and success is None:
        state = RuntimeHealth.UNAVAILABLE
        reasons.append("NO_REQUEST_EVIDENCE")
    elif success is None:
        state = RuntimeHealth.UNAVAILABLE
        reasons.append("NO_SUCCESSFUL_REQUEST")
    elif latest is not None and not latest.success and latest.started_at > success.started_at:
        state = RuntimeHealth.DEGRADED
        reasons.append("LATEST_REQUEST_FAILED_AFTER_SUCCESS")
    elif latest_run is not None and latest_run.status == "partial":
        state = RuntimeHealth.PARTIAL
        reasons.append("LATEST_SYNC_PARTIAL")
    else:
        state = RuntimeHealth.HEALTHY
    return HealthSnapshot(
        source=source,
        mapping_quality=MappingQuality.VALID,
        runtime_health=state,
        latest_success_at=success.finished_at if success is not None else None,
        reasons=reasons,
    )


def _mapping_for_policy(
    session: Session, policy: SourceRegistryPolicy
) -> IndicatorSourceMapping | None:
    if policy.primary_source is SourceSystem.FRED_CURRENT:
        return None
    source = storage_source_name(policy.primary_source.value)
    return session.scalar(
        select(IndicatorSourceMapping)
        .where(
            IndicatorSourceMapping.indicator_id == policy.indicator_id,
            IndicatorSourceMapping.source == source,
            IndicatorSourceMapping.source_series_id == policy.primary_source_identity,
            IndicatorSourceMapping.enabled.is_(True),
        )
        .order_by(IndicatorSourceMapping.id.desc())
        .limit(1)
    )


def indicator_source_health(
    session: Session,
    policy: SourceRegistryPolicy,
    *,
    today: date | None = None,
    evaluated_at: datetime | None = None,
) -> HealthSnapshot:
    """Compute mapping-aware expected-period health for one primary source."""
    if today is not None and evaluated_at is not None:
        raise ValueError("provide today or evaluated_at, not both")
    expectation = freshness_expectation(policy, today=today, evaluated_at=evaluated_at)
    evaluation_time = (
        evaluated_at.astimezone(UTC)
        if evaluated_at is not None
        else datetime.combine(today, time.max, tzinfo=UTC)
        if today is not None
        else datetime.now(UTC)
    )
    source = storage_source_name(policy.primary_source.value)
    mapping = _mapping_for_policy(session, policy)
    quality = (
        MappingQuality.VALID
        if mapping is None
        else mapping_quality_from_status(mapping.mapping_status)
    )
    expected = expectation.period
    latest_date = session.scalar(
        select(RawObservation.observation_date)
        .where(
            RawObservation.indicator_id == policy.indicator_id,
            RawObservation.source == source,
        )
        .order_by(RawObservation.observation_date.desc())
        .limit(1)
    )
    reasons: list[str] = []
    latest_success = _latest_request(session, source, success=True)

    if quality in {MappingQuality.INVALID, MappingQuality.PENDING_REVIEW}:
        state = RuntimeHealth.ERROR
        reasons.append("MAPPING_NOT_PRODUCTION_ELIGIBLE")
    elif quality is MappingQuality.UNAVAILABLE:
        state = RuntimeHealth.UNAVAILABLE
        reasons.append("MAPPING_UNAVAILABLE")
    elif (
        session.scalar(
            select(DataQualityEvent.id)
            .where(
                DataQualityEvent.indicator_id == policy.indicator_id,
                DataQualityEvent.event_type.in_(INTEGRITY_EVENT_TYPES),
                DataQualityEvent.resolved_at.is_(None),
            )
            .limit(1)
        )
        is not None
    ):
        state = RuntimeHealth.ERROR
        reasons.append("UNRESOLVED_SOURCE_INTEGRITY_EVENT")
    elif latest_date is None:
        state = RuntimeHealth.UNAVAILABLE
        reasons.append("NO_PRIMARY_OBSERVATIONS")
    elif latest_date < expected:
        if evaluation_time <= expectation.grace_ends_at.astimezone(UTC):
            state = RuntimeHealth.DEGRADED
            reasons.append("EXPECTED_PERIOD_LAG_WITHIN_GRACE")
        else:
            state = RuntimeHealth.STALE
            reasons.append("EXPECTED_PERIOD_LAG_BEYOND_GRACE")
    elif not expectation.schedule_is_certain:
        state = RuntimeHealth.DEGRADED
        reasons.append("RELEASE_SCHEDULE_UNAVAILABLE")
    else:
        provider = provider_health(session, source)
        if provider.runtime_health in {RuntimeHealth.PARTIAL, RuntimeHealth.DEGRADED}:
            state = provider.runtime_health
            reasons.extend(provider.reasons)
        elif provider.runtime_health is RuntimeHealth.UNAVAILABLE:
            state = RuntimeHealth.DEGRADED
            reasons.append("OBSERVATIONS_PRESENT_WITHOUT_CURRENT_REQUEST_EVIDENCE")
        else:
            state = RuntimeHealth.HEALTHY

    return HealthSnapshot(
        indicator_id=policy.indicator_id,
        source=source,
        mapping_quality=quality,
        runtime_health=state,
        expected_latest_period=expected,
        scheduled_release_at=expectation.scheduled_release_at,
        grace_ends_at=expectation.grace_ends_at,
        schedule_is_certain=expectation.schedule_is_certain,
        latest_observation_date=latest_date,
        latest_success_at=latest_success.finished_at if latest_success is not None else None,
        reasons=reasons,
    )


def historical_audit_event_is_current(session: Session, event: DataQualityEvent) -> bool:
    """Return false when a later provider success supersedes an immutable old failure."""
    if event.event_type not in {"source_business_error", "source_series_unavailable"}:
        return event.resolved_at is None
    source = event.details_json.get("source")
    if not isinstance(source, str):
        return event.resolved_at is None
    later_success = session.scalar(
        select(DataSourceRequest.id)
        .where(
            DataSourceRequest.source == source,
            DataSourceRequest.success.is_(True),
            DataSourceRequest.started_at > event.detected_at,
        )
        .limit(1)
    )
    return event.resolved_at is None and later_success is None
