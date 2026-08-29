"""Date-EOD release-event and month-end schedule construction."""

import calendar
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date

from app.validation.hashing import semantic_hash
from app.validation.schemas import (
    EvaluationSchedule,
    ReleaseCandidate,
    ReleaseEvent,
    SemanticInformationSet,
    ValidationCadence,
)


def candidate_dates(candidates: Iterable[ReleaseCandidate]) -> list[date]:
    """Return unique candidate EODs; same-day releases are deliberately coalesced."""
    return sorted({item.event_date for item in candidates})


def group_candidates(candidates: Iterable[ReleaseCandidate]) -> dict[date, list[ReleaseCandidate]]:
    """Group indicator candidates by EOD without reconstructing intraday order."""
    grouped: dict[date, list[ReleaseCandidate]] = defaultdict(list)
    for item in candidates:
        grouped[item.event_date].append(item)
    return {
        event_date: sorted(rows, key=lambda item: item.indicator_id)
        for event_date, rows in sorted(grouped.items())
    }


def build_release_event(
    event_date: date,
    candidates: Iterable[ReleaseCandidate],
    previous: Mapping[str, SemanticInformationSet | None],
    current: Mapping[str, SemanticInformationSet],
) -> ReleaseEvent | None:
    """Create one event only when at least one semantic information set changed."""
    indicator_ids = sorted({item.indicator_id for item in candidates})
    changed: list[str] = []
    for indicator_id in indicator_ids:
        current_item = current.get(indicator_id)
        previous_item = previous.get(indicator_id)
        if current_item is not None and (
            previous_item is None or previous_item.semantic_hash != current_item.semantic_hash
        ):
            changed.append(indicator_id)
    if not changed:
        return None
    previous_hashes = {
        indicator_id: (
            previous_item.semantic_hash
            if (previous_item := previous.get(indicator_id)) is not None
            else None
        )
        for indicator_id in changed
    }
    new_hashes = {indicator_id: current[indicator_id].semantic_hash for indicator_id in changed}
    payload = {
        "event_date": event_date,
        "changed_indicator_ids": changed,
        "previous_information_set_hashes": previous_hashes,
        "new_information_set_hashes": new_hashes,
    }
    return ReleaseEvent(**payload, event_hash=semantic_hash(payload))


def release_schedule(events: Iterable[ReleaseEvent]) -> EvaluationSchedule:
    """Build a deterministic release-event schedule from semantic events."""
    dates = sorted({item.event_date for item in events})
    payload = {"cadence": ValidationCadence.RELEASE_EVENT_EOD, "dates": dates}
    return EvaluationSchedule(**payload, schedule_hash=semantic_hash(payload))


def month_end_dates(start: date, end: date) -> list[date]:
    """Return calendar month-end dates in an inclusive window."""
    if start > end:
        raise ValueError("start must not be after end")
    cursor = date(start.year, start.month, 1)
    results: list[date] = []
    while cursor <= end:
        last = date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
        if start <= last <= end:
            results.append(last)
        cursor = date(
            cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1
        )
    return results


def month_end_schedule(start: date, end: date) -> EvaluationSchedule:
    """Build an independent month-end benchmark schedule."""
    dates = month_end_dates(start, end)
    payload = {"cadence": ValidationCadence.MONTH_END_EOD, "dates": dates}
    return EvaluationSchedule(**payload, schedule_hash=semantic_hash(payload))


def combined_schedule_hash(*schedules: EvaluationSchedule) -> str:
    """Bind a run to ordered, independently named schedules."""
    return semantic_hash(
        [
            {
                "cadence": item.cadence,
                "dates": item.dates,
                "schedule_hash": item.schedule_hash,
            }
            for item in schedules
        ]
    )
