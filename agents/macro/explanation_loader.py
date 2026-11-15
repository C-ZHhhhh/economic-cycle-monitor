"""Persisted-only local context access for the explanation agent."""

from __future__ import annotations

from datetime import date

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.macro.explanation_config import hash_payload
from agents.macro.explanation_schemas import (
    DependencyAvailability,
    LoadedExplanationContext,
    PersistedCadence,
    PersistedExplanationContext,
)
from app.composites.schemas import EvaluationMode
from app.database.models import AgentReport

PERSISTED_CONTEXT_REPORT_TYPE = "macro_explanation_context"


class PersistedContextError(RuntimeError):
    """A locally persisted explanation dependency failed validation."""


def persisted_context_hash(context: PersistedExplanationContext) -> str:
    """Hash a context without its self-referential identity field."""
    return hash_payload(context.model_dump(mode="json", exclude={"persisted_context_hash"}))


def seal_persisted_context(**values: object) -> PersistedExplanationContext:
    """Create a context with a deterministic content hash for upstream persistence."""
    pending = PersistedExplanationContext.model_validate(
        {**values, "persisted_context_hash": "pending"}
    )
    return pending.model_copy(update={"persisted_context_hash": persisted_context_hash(pending)})


def _authority_identity(context: PersistedExplanationContext) -> tuple[str, ...]:
    readiness = context.readiness
    return (
        context.regime.source_registry_hash,
        context.regime.feature_spec_version,
        context.regime.normalization_spec_version,
        context.regime.normalization_spec_hash,
        context.regime.pillar_spec_version,
        context.regime.pillar_spec_hash,
        context.regime.stage3d_spec_version,
        context.regime.stage3d_spec_hash,
        context.regime.regime_spec_version,
        context.regime.regime_spec_hash,
        (readiness.current_freshness_version or "") if readiness else "",
        (readiness.current_freshness_hash or "") if readiness else "",
        context.regime.revision_provenance_version or "",
        context.regime.revision_provenance_hash or "",
    )


class PersistedExplanationContextLoader:
    """Read validated Stage 3 outputs from AgentReport JSONB without provider access."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _contexts(self) -> list[PersistedExplanationContext]:
        statement = (
            select(AgentReport)
            .where(AgentReport.report_type == PERSISTED_CONTEXT_REPORT_TYPE)
            .order_by(AgentReport.created_at.desc(), AgentReport.id.desc())
        )
        contexts: list[PersistedExplanationContext] = []
        for report in self.session.scalars(statement):
            try:
                context = PersistedExplanationContext.model_validate(report.result_json)
            except ValidationError as exc:
                raise PersistedContextError("INVALID_PERSISTED_EXPLANATION_CONTEXT") from exc
            if persisted_context_hash(context) != context.persisted_context_hash:
                raise PersistedContextError("PERSISTED_EXPLANATION_CONTEXT_HASH_MISMATCH")
            contexts.append(context)
        return contexts

    def load(
        self,
        *,
        evaluation_mode: EvaluationMode,
        evaluation_as_of: date | None = None,
    ) -> LoadedExplanationContext:
        """Load current plus strict predecessor contexts from local JSONB only."""
        contexts = self._contexts()
        eligible = [item for item in contexts if item.evaluation_mode is evaluation_mode]
        if evaluation_as_of is not None:
            eligible = [item for item in eligible if item.evaluation_as_of == evaluation_as_of]
        if not eligible:
            return LoadedExplanationContext(
                status=DependencyAvailability.UPSTREAM_RESULT_NOT_MATERIALIZED,
                previous_release_eod_status=DependencyAvailability.COMPARISON_NOT_AVAILABLE,
                previous_month_end_status=DependencyAvailability.COMPARISON_NOT_AVAILABLE,
                trajectory_context_status=DependencyAvailability.TRAJECTORY_CONTEXT_NOT_AVAILABLE,
            )
        current = max(
            eligible,
            key=lambda item: (item.evaluation_as_of, item.materialized_at),
        )
        identity = _authority_identity(current)
        predecessors = [
            item
            for item in contexts
            if item.evaluation_mode is evaluation_mode
            and item.evaluation_as_of < current.evaluation_as_of
            and _authority_identity(item) == identity
        ]

        def latest(cadence: PersistedCadence) -> PersistedExplanationContext | None:
            candidates = [item for item in predecessors if item.cadence is cadence]
            return max(candidates, key=lambda item: item.evaluation_as_of, default=None)

        release = latest(PersistedCadence.RELEASE_EVENT_EOD)
        month_end = latest(PersistedCadence.MONTH_END_EOD)
        lag = current.lagged_macro_state
        lag_hashes = {
            current.regime.growth_trajectory.lag_state_hash,
            current.regime.inflation_trajectory.lag_state_hash,
        }
        trajectory_status = (
            DependencyAvailability.AVAILABLE
            if lag is not None and lag.calculation_hash in lag_hashes
            else DependencyAvailability.TRAJECTORY_CONTEXT_NOT_AVAILABLE
        )
        return LoadedExplanationContext(
            status=DependencyAvailability.AVAILABLE,
            current=current,
            previous_release_eod_status=(
                DependencyAvailability.AVAILABLE
                if release is not None
                else DependencyAvailability.COMPARISON_NOT_AVAILABLE
            ),
            previous_release_eod=release,
            previous_month_end_status=(
                DependencyAvailability.AVAILABLE
                if month_end is not None
                else DependencyAvailability.COMPARISON_NOT_AVAILABLE
            ),
            previous_month_end=month_end,
            trajectory_context_status=trajectory_status,
        )
