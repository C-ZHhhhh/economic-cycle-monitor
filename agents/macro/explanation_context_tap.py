"""Passive capture of already-computed Stage 3 results for explanation contexts."""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from datetime import UTC, date, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from agents.macro.explanation_loader import seal_persisted_context
from agents.macro.explanation_schemas import (
    PersistedCadence,
    PersistedExplanationContext,
)
from app.composites.schemas import EvaluationMode, MacroStateResult
from app.pillars.schemas import PillarResult
from app.regimes.schemas import RegimeResult
from app.signals.schemas import NormalizedSignalResult
from app.source_registry.config import load_source_registry
from app.source_registry.health import indicator_source_health
from app.source_registry.readiness import validate_data_layer
from app.source_registry.schemas import HealthSnapshot, ReadinessStatus


class SignalResultService(Protocol):
    """Stage 3B boundary observed by the passive context tap."""

    def compute_signal_set(
        self,
        signal_ids: Sequence[str],
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> Awaitable[list[NormalizedSignalResult]]:
        """Return normalized signal results from the wrapped service."""
        ...


class PillarResultService(Protocol):
    """Stage 3C boundary observed by the passive context tap."""

    def compute_all_pillars(
        self, evaluation_as_of: date | None = None
    ) -> Awaitable[list[PillarResult]]:
        """Return the complete pillar set from the wrapped service."""
        ...


class MacroStateResultService(Protocol):
    """Stage 3D boundary observed by the passive context tap."""

    def compute_macro_state(
        self, evaluation_as_of: date | None = None
    ) -> Awaitable[MacroStateResult]:
        """Return a macro state from the wrapped service."""
        ...


class PassiveStage3ContextTap:
    """Collect typed results without initiating computation or performing I/O."""

    def __init__(self) -> None:
        self._signals: dict[date | None, dict[str, NormalizedSignalResult]] = {}
        self._pillars: dict[date | None, tuple[PillarResult, ...]] = {}
        self._states: dict[str, MacroStateResult] = {}

    def record_signals(
        self,
        evaluation_context: date | None,
        rows: Sequence[NormalizedSignalResult],
    ) -> None:
        """Record the newest returned result per signal for one information set."""
        captured = self._signals.setdefault(evaluation_context, {})
        for row in rows:
            if row.as_of != evaluation_context:
                raise ValueError("PASSIVE_TAP_SIGNAL_CONTEXT_MISMATCH")
            previous = captured.get(row.signal_id)
            if previous is None or row.feature_observation_date > previous.feature_observation_date:
                captured[row.signal_id] = row.model_copy(deep=True)

    def record_pillars(
        self,
        evaluation_context: date | None,
        rows: Sequence[PillarResult],
    ) -> None:
        """Record one complete Stage 3C result set after normal computation."""
        self._pillars[evaluation_context] = tuple(
            item.model_copy(deep=True) for item in sorted(rows, key=lambda row: row.pillar_id)
        )

    def record_macro_state(self, row: MacroStateResult) -> None:
        """Record one already-computed Stage 3D result by calculation identity."""
        self._states[row.calculation_hash] = row.model_copy(deep=True)

    def build_context(
        self,
        regime: RegimeResult,
        *,
        cadence: PersistedCadence,
        readiness: ReadinessStatus | None = None,
        health: Sequence[HealthSnapshot] = (),
        release_event_id: str | None = None,
        materialized_at: datetime | None = None,
    ) -> PersistedExplanationContext:
        """Seal a context only when all required captured results are complete."""
        evaluation_context = (
            None if regime.evaluation_mode is EvaluationMode.CURRENT else regime.evaluation_as_of
        )
        macro_state = self._states.get(regime.macro_state_calculation_hash)
        lag_hashes = {
            regime.growth_trajectory.lag_state_hash,
            regime.inflation_trajectory.lag_state_hash,
        }
        lagged_states = [self._states[item] for item in lag_hashes if item in self._states]
        pillars = self._pillars.get(evaluation_context)
        signals = self._signals.get(evaluation_context)
        if macro_state is None or pillars is None or signals is None:
            raise RuntimeError("UPSTREAM_RESULT_NOT_MATERIALIZED")
        if len(pillars) != 9:
            raise RuntimeError("UPSTREAM_PILLAR_SET_INCOMPLETE")
        required_signal_ids = {
            signal_id for pillar in pillars for signal_id in pillar.constituent_signal_ids
        }
        if not required_signal_ids.issubset(signals):
            raise RuntimeError("UPSTREAM_SIGNAL_SET_INCOMPLETE")
        if len(lagged_states) != 1:
            raise RuntimeError("UPSTREAM_TRAJECTORY_CONTEXT_NOT_MATERIALIZED")
        if regime.evaluation_mode is EvaluationMode.CURRENT:
            if readiness is None:
                raise RuntimeError("CURRENT_CONTEXT_REQUIRES_READINESS")
        elif readiness is not None or health:
            raise RuntimeError("PIT_CONTEXT_CANNOT_CARRY_CURRENT_FRESHNESS")
        return seal_persisted_context(
            cadence=cadence,
            release_event_id=release_event_id,
            regime=regime.model_copy(deep=True),
            macro_state=macro_state,
            lagged_macro_state=lagged_states[0],
            pillars=pillars,
            signals=tuple(signals[item] for item in sorted(signals)),
            readiness=readiness,
            health=tuple(
                item.model_copy(deep=True)
                for item in sorted(health, key=lambda row: (row.indicator_id or "", row.source))
            ),
            materialized_at=materialized_at or datetime.now(UTC),
        )


class RecordingSignalService:
    """Delegate Stage 3B unchanged and passively record its returned rows."""

    def __init__(self, inner: SignalResultService, tap: PassiveStage3ContextTap) -> None:
        self.inner = inner
        self.tap = tap

    async def compute_signal_set(
        self,
        signal_ids: Sequence[str],
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> list[NormalizedSignalResult]:
        """Return the wrapped result exactly while recording a defensive copy."""
        rows = await self.inner.compute_signal_set(signal_ids, start, end, as_of)
        context = as_of if isinstance(as_of, date) and not isinstance(as_of, datetime) else None
        self.tap.record_signals(context, rows)
        return rows


class RecordingPillarService:
    """Delegate Stage 3C unchanged and passively record its returned rows."""

    def __init__(self, inner: PillarResultService, tap: PassiveStage3ContextTap) -> None:
        self.inner = inner
        self.tap = tap

    async def compute_all_pillars(self, evaluation_as_of: date | None = None) -> list[PillarResult]:
        """Return the wrapped pillar set exactly while recording a defensive copy."""
        rows = await self.inner.compute_all_pillars(evaluation_as_of)
        self.tap.record_pillars(evaluation_as_of, rows)
        return rows


class RecordingMacroStateService:
    """Delegate Stage 3D unchanged and passively record its returned result."""

    def __init__(self, inner: MacroStateResultService, tap: PassiveStage3ContextTap) -> None:
        self.inner = inner
        self.tap = tap

    async def compute_macro_state(self, evaluation_as_of: date | None = None) -> MacroStateResult:
        """Return the wrapped state exactly while recording a defensive copy."""
        row = await self.inner.compute_macro_state(evaluation_as_of)
        self.tap.record_macro_state(row)
        return row


def current_context_runtime_evidence(
    session: Session,
) -> tuple[ReadinessStatus, tuple[HealthSnapshot, ...]]:
    """Read current readiness and all indicator health snapshots without network I/O."""
    registry = load_source_registry()
    readiness = validate_data_layer(session, registry=registry)
    health = tuple(indicator_source_health(session, policy) for policy in registry.policies)
    return readiness, health
