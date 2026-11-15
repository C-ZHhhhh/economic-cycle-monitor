"""Compute one current-mode Stage 3 result and persist it as an explanation context.

This is the upstream orchestration the explanation agent reads from. It runs the
frozen Stage 3B-through-3E engines exactly once, lets the passive tap capture what
they returned, and stores the sealed context. It changes no calculation.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.macro.explanation_context_tap import (
    PassiveStage3ContextTap,
    RecordingMacroStateService,
    RecordingPillarService,
    RecordingSignalService,
    current_context_runtime_evidence,
)
from agents.macro.explanation_loader import (
    PERSISTED_CONTEXT_REPORT_TYPE,
    seal_persisted_context,
)
from agents.macro.explanation_persistence import persist_explanation_context
from agents.macro.explanation_schemas import PersistedCadence, PersistedExplanationContext
from app.composites.schemas import EvaluationMode
from app.composites.service import MacroStateEngine
from app.database.models import AgentReport
from app.pillars.service import PillarEngine
from app.regimes.service import MacroRegimeEngine
from app.signals.service import SignalEngine


async def compute_current_context(session: Session) -> PersistedExplanationContext:
    """Run the current-mode engine chain once and seal what it produced."""
    tap = PassiveStage3ContextTap()
    signals = RecordingSignalService(SignalEngine(session), tap)
    pillars = RecordingPillarService(PillarEngine(session, signal_engine=signals), tap)
    states = RecordingMacroStateService(MacroStateEngine(session, pillar_engine=pillars), tap)
    regime = await MacroRegimeEngine(session, macro_state_engine=states).compute_regime(None)
    readiness, health = current_context_runtime_evidence(session)
    return tap.build_context(
        regime,
        cadence=PersistedCadence.RELEASE_EVENT_EOD,
        readiness=readiness,
        health=health,
    )


def _current_reports(session: Session, cadence: PersistedCadence) -> list[AgentReport]:
    """Return persisted current-mode context rows for one cadence, newest date first."""
    as_of = AgentReport.result_json["regime"]["evaluation_as_of"].astext
    return list(
        session.scalars(
            select(AgentReport)
            .where(
                AgentReport.report_type == PERSISTED_CONTEXT_REPORT_TYPE,
                AgentReport.result_json["cadence"].astext == cadence.value,
                AgentReport.result_json["regime"]["evaluation_mode"].astext
                == EvaluationMode.CURRENT.value,
            )
            .order_by(as_of.desc(), AgentReport.created_at.desc())
        )
    )


def _economic_identity(payload: dict[str, Any]) -> tuple[object, ...]:
    """Return what a snapshot says, ignoring when its inputs were fetched.

    Current-mode calculation hashes include retrieval timestamps, so two runs over
    unchanged data hash differently. Scores, classification and pillar values are
    what decide whether a snapshot says anything new.
    """
    regime = payload["regime"]
    return (
        regime["growth_strength"],
        regime["inflation_pressure"],
        regime["base_regime_id"],
        regime["regime_status"],
        tuple(
            (item["pillar_id"], item["value"], item["pillar_status"]) for item in payload["pillars"]
        ),
    )


def persist_current_context(
    session: Session,
    context: PersistedExplanationContext,
    *,
    git_commit: str | None = None,
) -> UUID | None:
    """Store a new snapshot, or return ``None`` when the same result is already stored."""
    identity = _economic_identity(context.model_dump(mode="json"))
    for report in _current_reports(session, PersistedCadence.RELEASE_EVENT_EOD):
        if report.result_json["regime"]["evaluation_as_of"] != context.evaluation_as_of.isoformat():
            break
        if _economic_identity(report.result_json) == identity:
            return None
    return persist_explanation_context(session, context, git_commit=git_commit)


def promote_previous_month_end(
    session: Session,
    as_of: date,
    *,
    git_commit: str | None = None,
) -> date | None:
    """Keep the last snapshot of the previous month as that month's closing record.

    Current-mode results cannot be recomputed for a past date, so a month-end
    comparison is only possible if a closing record was kept. The newest snapshot
    taken before the first day of ``as_of``'s month is stored once more under the
    month-end cadence, unchanged and still labelled with its own evaluation date.
    Returns that date when a record was added.
    """
    month_start = as_of.replace(day=1).isoformat()
    candidate = next(
        (
            report
            for report in _current_reports(session, PersistedCadence.RELEASE_EVENT_EOD)
            if report.result_json["regime"]["evaluation_as_of"] < month_start
        ),
        None,
    )
    if candidate is None:
        return None
    closing_date = candidate.result_json["regime"]["evaluation_as_of"]
    if any(
        report.result_json["regime"]["evaluation_as_of"] == closing_date
        for report in _current_reports(session, PersistedCadence.MONTH_END_EOD)
    ):
        return None
    source = PersistedExplanationContext.model_validate(candidate.result_json)
    values = source.model_dump(exclude={"persisted_context_hash", "cadence"})
    month_end = seal_persisted_context(**values, cadence=PersistedCadence.MONTH_END_EOD)
    persist_explanation_context(session, month_end, git_commit=git_commit)
    return source.evaluation_as_of
