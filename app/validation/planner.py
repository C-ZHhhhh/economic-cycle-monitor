"""Bounded release-aware Stage 3F pilot planning."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from app.validation.hashing import semantic_hash
from app.validation.schedule import month_end_schedule
from app.validation.schemas import (
    EvaluationSchedule,
    MaterializationPlan,
    MaterializationUnit,
    PlannerDefinition,
    ReleaseCandidate,
    ValidationCadence,
    ValidationPhase,
)


def _window_score(
    candidates: Sequence[ReleaseCandidate],
    start: date,
    end: date,
    definition: PlannerDefinition,
) -> tuple[int, int, int, int]:
    selected = [item for item in candidates if start <= item.event_date <= end]
    frequencies = len({item.frequency for item in selected} & set(definition.require_frequencies))
    sources = len({item.source_family for item in selected})
    dates = len({item.event_date for item in selected})
    indicators = len({item.indicator_id for item in selected})
    bounded_dates = dates if dates <= definition.maximum_release_events else -dates
    return (
        frequencies,
        min(sources, definition.require_primary_source_families),
        bounded_dates,
        indicators,
    )


def propose_pilot_window(
    candidates: Iterable[ReleaseCandidate],
    definition: PlannerDefinition,
) -> tuple[date, date, list[ReleaseCandidate]]:
    """Choose a representative bounded window from real release candidates."""
    rows = sorted(candidates, key=lambda item: (item.event_date, item.indicator_id))
    if not rows:
        raise ValueError("PILOT_RELEASE_INVENTORY_EMPTY")
    unique_dates = sorted({item.event_date for item in rows})
    options: list[tuple[tuple[int, int, int, int], date, date]] = []
    for end in unique_dates:
        for days in range(definition.minimum_window_days, definition.maximum_window_days + 1):
            start = end - timedelta(days=days - 1)
            score = _window_score(rows, start, end, definition)
            options.append((score, start, end))
    _score, start, end = max(options, key=lambda item: (item[0], item[2], item[1]))
    selected = [item for item in rows if start <= item.event_date <= end]
    selected_dates = sorted({item.event_date for item in selected})
    if len(selected_dates) > definition.maximum_release_events:
        kept_dates = set(selected_dates[-definition.maximum_release_events :])
        selected = [item for item in selected if item.event_date in kept_dates]
        start = min(kept_dates)
        end = max(kept_dates)
    frequencies = {item.frequency for item in selected}
    if not set(definition.require_frequencies).issubset(frequencies):
        raise ValueError("PILOT_WINDOW_MISSING_REQUIRED_FREQUENCY")
    if len({item.source_family for item in selected}) < definition.require_primary_source_families:
        raise ValueError("PILOT_WINDOW_MISSING_SOURCE_DIVERSITY")
    return start, end, selected


def _unit(
    unit_type: str,
    *,
    indicator_id: str | None = None,
    as_of: date | None = None,
    cadence: ValidationCadence | None = None,
) -> MaterializationUnit:
    payload = {
        "unit_type": unit_type,
        "indicator_id": indicator_id,
        "as_of": as_of,
        "cadence": cadence,
    }
    return MaterializationUnit(unit_id=semantic_hash(payload), **payload)


def build_pilot_plan(
    *,
    release_schedule: EvaluationSchedule,
    window_start: date,
    window_end: date,
    indicator_ids: Sequence[str],
    trajectory_horizon_months: int,
    extra_snapshot_dates: Sequence[date] = (),
    phase: ValidationPhase = ValidationPhase.PILOT,
) -> MaterializationPlan:
    """Build exact snapshot and production replay units for an approved pilot."""
    if phase is not ValidationPhase.PILOT:
        raise ValueError("FULL_HISTORICAL_RUN_NOT_APPROVED")
    benchmark = month_end_schedule(window_start, window_end)
    evaluation_dates = sorted(set(release_schedule.dates) | set(benchmark.dates))
    lag_dates = sorted(
        {item - relativedelta(months=trajectory_horizon_months) for item in evaluation_dates}
    )
    snapshot_dates = sorted(set(evaluation_dates) | set(lag_dates) | set(extra_snapshot_dates))
    units: list[MaterializationUnit] = [
        _unit("VINTAGE_DISCOVERY", indicator_id=indicator_id)
        for indicator_id in sorted(indicator_ids)
    ]
    for as_of in snapshot_dates:
        for indicator_id in sorted(indicator_ids):
            units.append(_unit("EXACT_PIT_SNAPSHOT", indicator_id=indicator_id, as_of=as_of))
    for as_of in release_schedule.dates:
        units.append(
            _unit(
                "PRODUCTION_REPLAY",
                as_of=as_of,
                cadence=ValidationCadence.RELEASE_EVENT_EOD,
            )
        )
    for as_of in benchmark.dates:
        units.append(
            _unit(
                "PRODUCTION_REPLAY",
                as_of=as_of,
                cadence=ValidationCadence.MONTH_END_EOD,
            )
        )
    plan_payload = {
        "phase": phase,
        "window_start": window_start,
        "window_end": window_end,
        "release_schedule": release_schedule.model_dump(mode="json"),
        "month_end_schedule": benchmark.model_dump(mode="json"),
        "lag_endpoint_dates": lag_dates,
        "units": [item.model_dump(mode="json") for item in units],
    }
    return MaterializationPlan(
        **plan_payload,
        planned_logical_pit_units=len(units),
        plan_hash=semantic_hash(plan_payload),
    )


def estimate_full_run(
    *,
    pilot_units: int,
    pilot_requests: int,
    pilot_storage_bytes: int,
    pilot_runtime_seconds: DecimalLike,
    pilot_evaluations: int,
    full_evaluations: int,
) -> dict[str, int | float]:
    """Scale observed pilot workload without authorizing or executing it."""
    if pilot_evaluations <= 0:
        raise ValueError("pilot_evaluations must be positive")
    factor = full_evaluations / pilot_evaluations
    return {
        "estimated_full_logical_units": round(pilot_units * factor),
        "estimated_full_external_requests": round(pilot_requests * factor),
        "estimated_full_storage_bytes": round(pilot_storage_bytes * factor),
        "estimated_full_runtime_seconds": round(float(pilot_runtime_seconds) * factor, 3),
    }


DecimalLike = int | float | str
