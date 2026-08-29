"""Pure transition, occupancy, duration, and workload metrics."""

from collections import Counter
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from app.validation.schemas import RegimeHistoryRow


def transition_counts(rows: Sequence[RegimeHistoryRow]) -> dict[str, int]:
    """Count base and independent condition/trajectory transitions."""
    ordered = sorted(rows, key=lambda item: item.evaluation_as_of)
    counters = {
        "base_regime": 0,
        "growth_condition": 0,
        "inflation_condition": 0,
        "growth_trajectory": 0,
        "inflation_trajectory": 0,
    }
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if previous.base_regime_id != current.base_regime_id:
            counters["base_regime"] += 1
        if previous.growth_condition != current.growth_condition:
            counters["growth_condition"] += 1
        if previous.inflation_condition != current.inflation_condition:
            counters["inflation_condition"] += 1
        if previous.growth_trajectory != current.growth_trajectory:
            counters["growth_trajectory"] += 1
        if previous.inflation_trajectory != current.inflation_trajectory:
            counters["inflation_trajectory"] += 1
    return counters


def event_count_occupancy(rows: Sequence[RegimeHistoryRow]) -> dict[str, Decimal]:
    """Return regime share by evaluation count, not elapsed time."""
    eligible = [item.base_regime_id or "UNCLASSIFIED" for item in rows]
    if not eligible:
        return {}
    counts = Counter(eligible)
    total = Decimal(len(eligible))
    return {key: Decimal(value) / total for key, value in sorted(counts.items())}


def calendar_duration_occupancy(
    rows: Sequence[RegimeHistoryRow], *, through: date | None = None
) -> dict[str, Decimal]:
    """Return regime share by calendar days between explicit evaluation EODs."""
    ordered = sorted(rows, key=lambda item: item.evaluation_as_of)
    if not ordered:
        return {}
    end = through or ordered[-1].evaluation_as_of
    days: Counter[str] = Counter()
    for index, current in enumerate(ordered):
        next_date = ordered[index + 1].evaluation_as_of if index + 1 < len(ordered) else end
        duration = max(0, (next_date - current.evaluation_as_of).days)
        days[current.base_regime_id or "UNCLASSIFIED"] += duration
    total = sum(days.values())
    if total == 0:
        return {key: Decimal(0) for key in sorted(days)}
    return {key: Decimal(value) / Decimal(total) for key, value in sorted(days.items())}


def regime_durations(rows: Sequence[RegimeHistoryRow]) -> list[dict[str, object]]:
    """Return consecutive base-regime runs in evaluation and calendar units."""
    ordered = sorted(rows, key=lambda item: item.evaluation_as_of)
    if not ordered:
        return []
    output: list[dict[str, object]] = []
    start_index = 0
    for index in range(1, len(ordered) + 1):
        changed = index == len(ordered) or (
            ordered[index].base_regime_id != ordered[start_index].base_regime_id
        )
        if not changed:
            continue
        first = ordered[start_index]
        last = ordered[index - 1]
        output.append(
            {
                "base_regime_id": first.base_regime_id,
                "start": first.evaluation_as_of,
                "end": last.evaluation_as_of,
                "evaluation_count": index - start_index,
                "calendar_days": (last.evaluation_as_of - first.evaluation_as_of).days,
            }
        )
        start_index = index
    return output
