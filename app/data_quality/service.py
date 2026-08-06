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
from app.data_quality.repository import persist_quality_issues
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
        issues.extend(
            check_freshness(
                indicator.indicator_id,
                latest_date,
                max_staleness_days=indicator.max_staleness_days,
            )
        )
        for observation in observations:
            if not Decimal(observation.value).is_finite():
                issues.append(
                    QualityIssue(
                        indicator_id=indicator.indicator_id,
                        event_type="invalid_numeric_value",
                        severity="error",
                        message="Stored observation value is not finite",
                        details={"observation_date": observation.observation_date},
                    )
                )
        vintages = session.scalars(
            select(ObservationVintage).where(
                ObservationVintage.indicator_id == indicator.indicator_id
            )
        ).all()
        for vintage in vintages:
            if vintage.previous_value is not None:
                issues.extend(
                    check_revision(
                        indicator.indicator_id,
                        previous_value=Decimal(vintage.previous_value),
                        current_value=Decimal(vintage.current_value),
                        observation_date=vintage.observation_date,
                    )
                )
        duplicates = session.execute(
            select(
                ObservationVintage.observation_date,
                ObservationVintage.vintage_date,
                func.count(ObservationVintage.id),
            )
            .where(ObservationVintage.indicator_id == indicator.indicator_id)
            .group_by(ObservationVintage.observation_date, ObservationVintage.vintage_date)
            .having(func.count(ObservationVintage.id) > 1)
        ).all()
        issues.extend(
            duplicate_vintage_issue(
                indicator.indicator_id,
                observation_date=observation_date,
                vintage_date=vintage_date,
            )
            for observation_date, vintage_date, _count in duplicates
        )
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
    issues.extend(
        raw_hash_conflict_issue(
            indicator_by_series.get(series_id),
            response_path=response_path,
            hashes=list(hashes),
        )
        for series_id, response_path, hashes in conflicts
        if response_path is not None
    )
    inserted = persist_quality_issues(session, issues, run_id=run.run_id)
    summary = SyncSummary(
        run_id=run.run_id,
        operation=run.operation,
        status="succeeded",
        indicator_count=len(indicators),
        success_count=len(indicators),
        quality_event_count=inserted,
        results=[{"issue_count": len(issues), "inserted_count": inserted}],
    )
    finish_sync_run(session, run, summary)
    return summary


def quality_summary_for_indicator(session: Session, indicator_id: str) -> dict[str, Any]:
    """Return compact unresolved counts by severity for an indicator detail response."""
    rows = session.execute(
        select(DataQualityEvent.severity, func.count(DataQualityEvent.id))
        .where(
            DataQualityEvent.indicator_id == indicator_id,
            DataQualityEvent.resolved_at.is_(None),
        )
        .group_by(DataQualityEvent.severity)
    ).all()
    return {severity: count for severity, count in rows}
