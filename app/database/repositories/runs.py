"""Repository operations for agent runs and reports."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import AgentReport, AgentRun


def add_run_with_report(
    session: Session,
    run: AgentRun,
    report: AgentReport,
) -> AgentRun:
    """Persist a run and its report in the caller's transaction."""
    session.add(run)
    session.flush()
    report.run_id = run.run_id
    session.add(report)
    session.flush()
    return run


def list_runs(session: Session, *, offset: int = 0, limit: int = 50) -> Sequence[AgentRun]:
    """Return recent runs in reverse chronological order."""
    statement = select(AgentRun).order_by(AgentRun.started_at.desc()).offset(offset).limit(limit)
    return session.scalars(statement).all()


def get_run(session: Session, run_id: UUID) -> AgentRun | None:
    """Return one run by UUID."""
    return session.get(AgentRun, run_id)


def get_reports_for_run(session: Session, run_id: UUID) -> Sequence[AgentReport]:
    """Return reports associated with a run."""
    statement = (
        select(AgentReport)
        .where(AgentReport.run_id == run_id)
        .order_by(AgentReport.created_at.asc())
    )
    return session.scalars(statement).all()


def get_report_for_run_type(
    session: Session,
    run_id: UUID,
    report_type: str,
) -> AgentReport | None:
    """Return one typed report for a run without triggering any computation."""
    statement = select(AgentReport).where(
        AgentReport.run_id == run_id,
        AgentReport.report_type == report_type,
    )
    return session.scalar(statement)


def get_latest_report_by_type(session: Session, report_type: str) -> AgentReport | None:
    """Return the newest persisted report of one type."""
    statement = (
        select(AgentReport)
        .where(AgentReport.report_type == report_type)
        .order_by(AgentReport.created_at.desc(), AgentReport.id.desc())
        .limit(1)
    )
    return session.scalar(statement)
