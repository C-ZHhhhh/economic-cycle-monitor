"""Repository operations for agent runs and reports."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Select, select
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
    statement: Select[tuple[AgentRun]] = (
        select(AgentRun).order_by(AgentRun.started_at.desc()).offset(offset).limit(limit)
    )
    return session.scalars(statement).all()


def get_run(session: Session, run_id: UUID) -> AgentRun | None:
    """Return one run by UUID."""
    return session.get(AgentRun, run_id)


def get_reports_for_run(session: Session, run_id: UUID) -> Sequence[AgentReport]:
    """Return reports associated with a run."""
    statement: Select[tuple[AgentReport]] = (
        select(AgentReport)
        .where(AgentReport.run_id == run_id)
        .order_by(AgentReport.created_at.asc())
    )
    return session.scalars(statement).all()
