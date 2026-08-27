"""Database-wide Stage 2 quality-check orchestration."""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_pipeline.repository import finish_sync_run, start_sync_run
from app.data_pipeline.schemas import PipelineSelection, SyncSummary
from app.data_quality.checks import (
    QualityIssue,
    check_freshness,
    check_revision,
    duplicate_vintage_issue,
    raw_hash_conflict_issue,
)
from app.data_quality.repository import persist_quality_issues, reconcile_quality_issues
from app.data_quality.scope import QualityScope, quality_event_scope
from app.database.models import (
    DataQualityEvent,
    DataSourceRequest,
    ObservationVintage,
    RawObservation,
)
from app.indicators.repository import list_indicators


def run_database_quality_checks(
    session: Session, selection: PipelineSelection, *, since: date | None = None
) -> SyncSummary:
    """Run all persistence-level checks and write new unresolved events."""
    indicators = list_indicators(
        session,
        module=selection.module,
        enabled=True if selection.enabled_only else None,
        limit=100,
    )
    if selection.indicator_id is not None:
        indicators = [item for item in indicators if item.indicator_id == selection.indicator_id]
    run = start_sync_run(
        session,
        "check_data_quality",
        {**selection.model_dump(mode="json"), "since": since},
        len(indicators),
    )
    issues: list[QualityIssue] = []
    issues_to_persist: list[QualityIssue] = []
    inserted = 0
    resolved = 0
    for indicator in indicators:
        observations_statement = select(RawObservation).where(
            RawObservation.indicator_id == indicator.indicator_id
        )
        if since is not None:
            observations_statement = observations_statement.where(
                RawObservation.observation_date >= since
            )
        observations = session.scalars(
            observations_statement.order_by(RawObservation.observation_date)
        ).all()
        latest_date = observations[-1].observation_date if observations else None
        freshness_issues = check_freshness(
            indicator.indicator_id,
            latest_date,
            max_staleness_days=indicator.max_staleness_days,
        )
        issues.extend(freshness_issues)
        freshness_inserted, freshness_resolved = reconcile_quality_issues(
            session,
            freshness_issues,
            run_id=run.run_id,
            indicator_id=indicator.indicator_id,
            event_types=("stale_series", "no_recent_observations"),
        )
        inserted += freshness_inserted
        resolved += freshness_resolved
        for observation in observations:
            if not Decimal(observation.value).is_finite():
                issue = QualityIssue(
                    indicator_id=indicator.indicator_id,
                    event_type="invalid_numeric_value",
                    severity="error",
                    message="Stored observation value is not finite",
                    details={"observation_date": observation.observation_date},
                )
                issues.append(issue)
                issues_to_persist.append(issue)
        vintages = session.scalars(
            select(ObservationVintage).where(
                ObservationVintage.indicator_id == indicator.indicator_id
            )
        ).all()
        for vintage in vintages:
            if vintage.previous_value is not None:
                revision_issues = check_revision(
                    indicator.indicator_id,
                    previous_value=Decimal(vintage.previous_value),
                    current_value=Decimal(vintage.current_value),
                    observation_date=vintage.observation_date,
                )
                issues.extend(revision_issues)
                issues_to_persist.extend(revision_issues)
        duplicates = session.execute(
            select(
                ObservationVintage.observation_date,
                ObservationVintage.vintage_date,
                ObservationVintage.revision_number,
                ObservationVintage.source,
                func.count(ObservationVintage.id),
            )
            .where(ObservationVintage.indicator_id == indicator.indicator_id)
            .group_by(
                ObservationVintage.observation_date,
                ObservationVintage.vintage_date,
                ObservationVintage.revision_number,
                ObservationVintage.source,
            )
            .having(func.count(ObservationVintage.id) > 1)
        ).all()
        duplicate_issues = [
            duplicate_vintage_issue(
                indicator.indicator_id,
                observation_date=observation_date,
                vintage_date=vintage_date,
                revision_number=revision_number,
                source=source,
            )
            for observation_date, vintage_date, revision_number, source, _count in duplicates
        ]
        issues.extend(duplicate_issues)
        duplicate_inserted, duplicate_resolved = reconcile_quality_issues(
            session,
            duplicate_issues,
            run_id=run.run_id,
            indicator_id=indicator.indicator_id,
            event_types=("duplicate_vintage",),
        )
        inserted += duplicate_inserted
        resolved += duplicate_resolved
    conflicts = session.execute(
        select(
            DataSourceRequest.series_id,
            DataSourceRequest.response_path,
            func.array_agg(func.distinct(DataSourceRequest.response_hash)),
        )
        .where(DataSourceRequest.response_path.is_not(None))
        .group_by(DataSourceRequest.series_id, DataSourceRequest.response_path)
        .having(func.count(func.distinct(DataSourceRequest.response_hash)) > 1)
    ).all()
    indicator_by_series = {
        item.fred_series_id: item.indicator_id
        for item in indicators
        if item.fred_series_id is not None
    }
    conflict_issues = [
        raw_hash_conflict_issue(
            indicator_by_series.get(series_id),
            response_path=response_path,
            hashes=list(hashes),
        )
        for series_id, response_path, hashes in conflicts
        if response_path is not None
    ]
    issues.extend(conflict_issues)
    issues_to_persist.extend(conflict_issues)
    inserted += persist_quality_issues(session, issues_to_persist, run_id=run.run_id)
    summary = SyncSummary(
        run_id=run.run_id,
        operation=run.operation,
        status="succeeded",
        indicator_count=len(indicators),
        success_count=len(indicators),
        quality_event_count=inserted,
        results=[
            {
                "issue_count": len(issues),
                "inserted_count": inserted,
                "resolved_count": resolved,
            }
        ],
    )
    finish_sync_run(session, run, summary)
    return summary


def quality_summary_for_indicator(
    session: Session,
    indicator_id: str,
    *,
    quality_scope: QualityScope = QualityScope.CURRENT_RUNTIME,
) -> dict[str, Any]:
    """Return compact unresolved counts by severity for an indicator detail response."""
    events = session.scalars(
        select(DataQualityEvent).where(
            DataQualityEvent.indicator_id == indicator_id,
            DataQualityEvent.resolved_at.is_(None),
        )
    ).all()
    counts: dict[str, int] = {}
    for event in events:
        if quality_event_scope(session, event) is not quality_scope:
            continue
        counts[event.severity] = counts.get(event.severity, 0) + 1
    return counts
