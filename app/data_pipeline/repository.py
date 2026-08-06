"""PostgreSQL persistence for FRED requests, observations, revisions, and runs."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_pipeline.schemas import ObservationWriteResult, SyncSummary
from app.data_sources.fred.exceptions import FredRequestError
from app.data_sources.fred.schemas import FredObservation, FredPayload
from app.database.models import (
    DataSourceRequest,
    IndicatorRegistry,
    ObservationVintage,
    RawObservation,
    SyncRun,
)


def start_sync_run(
    session: Session, operation: str, parameters: dict[str, Any], indicator_count: int
) -> SyncRun:
    """Create a running audit record with credential-free parameters."""
    record = SyncRun(
        operation=operation,
        status="running",
        started_at=datetime.now(UTC),
        parameters_json=parameters,
        indicator_count=indicator_count,
    )
    session.add(record)
    session.flush()
    return record


def finish_sync_run(session: Session, record: SyncRun, summary: SyncSummary) -> None:
    """Finalize run counters from the public result."""
    record.status = summary.status
    record.finished_at = datetime.now(UTC)
    record.success_count = summary.success_count
    record.failure_count = summary.failure_count
    record.skipped_count = summary.skipped_count
    record.inserted_count = summary.inserted_count
    record.revised_count = summary.revised_count
    record.quality_event_count = summary.quality_event_count
    record.request_count = summary.request_count
    record.cache_hit_count = summary.cache_hit_count
    record.error_summary_json = [error.model_dump() for error in summary.errors]
    session.flush()


def record_successful_request(session: Session, run_id: UUID, payload: FredPayload) -> None:
    """Persist a successful request without secrets."""
    session.add(
        DataSourceRequest(
            sync_run_id=run_id,
            source="fred",
            endpoint=payload.endpoint,
            series_id=payload.series_id,
            request_parameters_json=payload.sanitized_parameters,
            started_at=payload.started_at,
            finished_at=payload.finished_at,
            http_status=payload.status_code,
            success=True,
            attempt_count=payload.attempt_count,
            response_hash=payload.response_hash,
            response_path=str(payload.response_path) if payload.response_path else None,
            from_cache=payload.from_cache,
        )
    )


def record_failed_request(
    session: Session,
    run_id: UUID,
    *,
    endpoint: str,
    series_id: str,
    parameters: dict[str, Any],
    error: Exception,
) -> None:
    """Persist sanitized failure evidence when no successful payload exists."""
    now = datetime.now(UTC)
    safe = {key: value for key, value in parameters.items() if key.lower() != "api_key"}
    status_code = error.status_code if isinstance(error, FredRequestError) else None
    attempt_count = error.attempt_count if isinstance(error, FredRequestError) else 1
    session.add(
        DataSourceRequest(
            sync_run_id=run_id,
            source="fred",
            endpoint=endpoint,
            series_id=series_id,
            request_parameters_json=safe,
            started_at=now,
            finished_at=now,
            http_status=status_code,
            success=False,
            attempt_count=attempt_count,
            error_type=type(error).__name__,
            error_message=str(error),
        )
    )


def latest_observation_date(session: Session, indicator_id: str) -> date | None:
    """Return the newest current observation date."""
    return session.scalar(
        select(func.max(RawObservation.observation_date)).where(
            RawObservation.indicator_id == indicator_id,
            RawObservation.source == "fred",
        )
    )


def persist_observations(
    session: Session,
    *,
    indicator: IndicatorRegistry,
    observations: list[FredObservation],
    payload_hash: str,
    retrieved_at: datetime,
    vintage_date: date | None = None,
    dry_run: bool = False,
    batch_size: int = 1000,
) -> ObservationWriteResult:
    """Insert current values and append revisions without losing prior values."""
    result = ObservationWriteResult()
    dated = [item for item in observations if item.value is not None]
    result.missing = len(observations) - len(dated)
    if not dated:
        return result
    dates = [item.date for item in dated]
    current = {
        item.observation_date: item
        for item in session.scalars(
            select(RawObservation).where(
                RawObservation.indicator_id == indicator.indicator_id,
                RawObservation.source == "fred",
                RawObservation.observation_date.in_(dates),
            )
        ).all()
    }
    for position, item in enumerate(dated, start=1):
        assert item.value is not None
        existing = current.get(item.date)
        if existing is None:
            result.inserted += 1
            if not dry_run:
                session.add(
                    RawObservation(
                        indicator_id=indicator.indicator_id,
                        observation_date=item.date,
                        value=item.value,
                        source="fred",
                        retrieved_at=retrieved_at,
                        raw_payload_hash=payload_hash,
                        source_vintage_date=vintage_date or item.realtime_start,
                    )
                )
                if position % batch_size == 0:
                    session.flush()
            continue
        if Decimal(existing.value) == item.value:
            result.unchanged += 1
            continue
        result.revised += 1
        if dry_run:
            continue
        revision_number = session.scalar(
            select(func.coalesce(func.max(ObservationVintage.revision_number), 0)).where(
                ObservationVintage.indicator_id == indicator.indicator_id,
                ObservationVintage.observation_date == item.date,
                ObservationVintage.source == "fred",
            )
        )
        session.add(
            ObservationVintage(
                indicator_id=indicator.indicator_id,
                observation_date=item.date,
                vintage_date=vintage_date or item.realtime_start,
                release_date=None,
                available_at=retrieved_at,
                release_stage="revision",
                revision_number=int(revision_number or 0) + 1,
                previous_value=existing.value,
                current_value=item.value,
                source="fred",
                source_vintage_date=vintage_date or item.realtime_start,
                retrieved_at=retrieved_at,
                raw_payload_hash=payload_hash,
            )
        )
        existing.value = item.value
        existing.retrieved_at = retrieved_at
        existing.raw_payload_hash = payload_hash
        existing.source_vintage_date = vintage_date or item.realtime_start
        if not dry_run and position % batch_size == 0:
            session.flush()
    session.flush()
    return result


def persist_as_known_observations(
    session: Session,
    *,
    indicator: IndicatorRegistry,
    observations: list[FredObservation],
    vintage_date: date,
    payload_hash: str,
    retrieved_at: datetime,
) -> ObservationWriteResult:
    """Store a requested ALFRED snapshot without replacing current observations."""
    result = ObservationWriteResult()
    for item in observations:
        if item.value is None:
            result.missing += 1
            continue
        exists = session.scalar(
            select(ObservationVintage.id).where(
                ObservationVintage.indicator_id == indicator.indicator_id,
                ObservationVintage.observation_date == item.date,
                ObservationVintage.vintage_date == vintage_date,
                ObservationVintage.source == "fred",
            )
        )
        if exists is not None:
            result.unchanged += 1
            continue
        max_revision = session.scalar(
            select(func.coalesce(func.max(ObservationVintage.revision_number), 0)).where(
                ObservationVintage.indicator_id == indicator.indicator_id,
                ObservationVintage.observation_date == item.date,
                ObservationVintage.source == "fred",
            )
        )
        session.add(
            ObservationVintage(
                indicator_id=indicator.indicator_id,
                observation_date=item.date,
                vintage_date=vintage_date,
                release_stage="as_known_on",
                revision_number=int(max_revision or 0) + 1,
                current_value=item.value,
                source="fred",
                source_vintage_date=vintage_date,
                retrieved_at=retrieved_at,
                raw_payload_hash=payload_hash,
            )
        )
        result.inserted += 1
    session.flush()
    return result
