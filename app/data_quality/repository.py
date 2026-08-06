"""Persistence and queries for data-quality events."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.data_quality.checks import QualityIssue
from app.database.models import DataQualityEvent


def persist_quality_issues(
    session: Session, issues: Sequence[QualityIssue], *, run_id: UUID | None
) -> int:
    """Insert findings idempotently while the same fingerprint remains unresolved."""
    inserted = 0
    for issue in issues:
        serialized = issue.model_dump(mode="json")
        existing = session.scalar(
            select(DataQualityEvent.id).where(
                DataQualityEvent.event_fingerprint == issue.fingerprint,
                DataQualityEvent.resolved_at.is_(None),
            )
        )
        if existing is not None:
            continue
        session.add(
            DataQualityEvent(
                run_id=run_id,
                indicator_id=issue.indicator_id,
                event_type=issue.event_type,
                severity=issue.severity,
                message=issue.message,
                details_json=serialized["details"],
                event_fingerprint=issue.fingerprint,
                detected_at=datetime.now(UTC),
            )
        )
        inserted += 1
    session.flush()
    return inserted


def reconcile_quality_issues(
    session: Session,
    issues: Sequence[QualityIssue],
    *,
    run_id: UUID | None,
    indicator_id: str,
    event_types: Sequence[str],
) -> tuple[int, int]:
    """Persist current findings and resolve scoped findings no longer present."""
    active_fingerprints = {issue.fingerprint for issue in issues}
    now = datetime.now(UTC)
    unresolved = session.scalars(
        select(DataQualityEvent).where(
            DataQualityEvent.indicator_id == indicator_id,
            DataQualityEvent.event_type.in_(event_types),
            DataQualityEvent.resolved_at.is_(None),
        )
    ).all()
    resolved = 0
    for event in unresolved:
        if event.event_fingerprint in active_fingerprints:
            continue
        event.resolved_at = now
        event.resolution_notes = "Condition was absent from the latest scoped validation"
        resolved += 1
    inserted = persist_quality_issues(session, issues, run_id=run_id)
    return inserted, resolved


def list_quality_events(
    session: Session,
    *,
    indicator_id: str | None = None,
    severity: str | None = None,
    event_type: str | None = None,
    unresolved_only: bool = False,
    offset: int = 0,
    limit: int = 100,
) -> Sequence[DataQualityEvent]:
    """List bounded quality events for API and CLI consumers."""
    statement: Select[tuple[DataQualityEvent]] = select(DataQualityEvent)
    if indicator_id is not None:
        statement = statement.where(DataQualityEvent.indicator_id == indicator_id)
    if severity is not None:
        statement = statement.where(DataQualityEvent.severity == severity)
    if event_type is not None:
        statement = statement.where(DataQualityEvent.event_type == event_type)
    if unresolved_only:
        statement = statement.where(DataQualityEvent.resolved_at.is_(None))
    statement = statement.order_by(DataQualityEvent.detected_at.desc()).offset(offset).limit(limit)
    return session.scalars(statement).all()
