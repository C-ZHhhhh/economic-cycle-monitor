"""Release-event, month-end, and pilot-planner tests."""

from datetime import date

import pytest

from app.validation.config import load_validation_catalog
from app.validation.planner import build_pilot_plan, propose_pilot_window
from app.validation.schedule import (
    build_release_event,
    combined_schedule_hash,
    group_candidates,
    month_end_dates,
    month_end_schedule,
    release_schedule,
)
from app.validation.schemas import (
    EvaluationSchedule,
    ReleaseCandidate,
    SemanticInformationSet,
    ValidationCadence,
    ValidationPhase,
)


def _candidate(
    day: int, indicator: str, frequency: str = "monthly", source: str = "BLS"
) -> ReleaseCandidate:
    return ReleaseCandidate(
        event_date=date(2026, 1, day),
        indicator_id=indicator,
        frequency=frequency,
        source_family=source,
    )


def _snapshot(indicator: str, day: int, digest: str) -> SemanticInformationSet:
    return SemanticInformationSet(
        indicator_id=indicator,
        as_of=date(2026, 1, day),
        observation_count=2,
        latest_observation_date=date(2025, 12, 1),
        semantic_hash=digest,
    )


def test_same_day_multi_release_is_coalesced_without_intraday_states() -> None:
    grouped = group_candidates([_candidate(5, "A"), _candidate(5, "B"), _candidate(6, "C")])
    assert list(grouped) == [date(2026, 1, 5), date(2026, 1, 6)]
    assert [item.indicator_id for item in grouped[date(2026, 1, 5)]] == ["A", "B"]


def test_semantic_event_is_absent_when_hash_did_not_change() -> None:
    event = build_release_event(
        date(2026, 1, 5),
        [_candidate(5, "A")],
        {"A": _snapshot("A", 4, "same")},
        {"A": _snapshot("A", 5, "same")},
    )
    assert event is None


def test_semantic_event_hash_changes_on_revision() -> None:
    first = build_release_event(
        date(2026, 1, 5),
        [_candidate(5, "A")],
        {"A": _snapshot("A", 4, "old")},
        {"A": _snapshot("A", 5, "new")},
    )
    revised = build_release_event(
        date(2026, 1, 5),
        [_candidate(5, "A")],
        {"A": _snapshot("A", 4, "old")},
        {"A": _snapshot("A", 5, "revised")},
    )
    assert first is not None and revised is not None
    assert first.event_hash != revised.event_hash


def test_semantic_event_ignores_unrepresented_runtime_timestamps() -> None:
    first = build_release_event(
        date(2026, 1, 5),
        [_candidate(5, "A")],
        {"A": _snapshot("A", 4, "old")},
        {"A": _snapshot("A", 5, "new")},
    )
    second = build_release_event(
        date(2026, 1, 5),
        [_candidate(5, "A")],
        {"A": _snapshot("A", 4, "old")},
        {"A": _snapshot("A", 5, "new")},
    )
    assert first is not None and second is not None
    assert first.event_hash == second.event_hash


def test_month_end_schedule_handles_leap_year() -> None:
    assert month_end_dates(date(2024, 1, 15), date(2024, 3, 2)) == [
        date(2024, 1, 31),
        date(2024, 2, 29),
    ]


def test_release_and_month_end_schedule_hashes_remain_independent() -> None:
    event = build_release_event(
        date(2026, 1, 5),
        [_candidate(5, "A")],
        {"A": _snapshot("A", 4, "old")},
        {"A": _snapshot("A", 5, "new")},
    )
    assert event is not None
    releases = release_schedule([event])
    months = month_end_schedule(date(2026, 1, 1), date(2026, 1, 31))
    assert releases.schedule_hash != months.schedule_hash
    assert combined_schedule_hash(releases, months) != releases.schedule_hash


def test_pilot_planner_requires_weekly_monthly_quarterly_and_multiple_sources() -> None:
    definition = load_validation_catalog().pilot_planner
    candidates = [
        _candidate(1, "W", "weekly", "DOL"),
        _candidate(2, "M", "monthly", "BLS"),
        _candidate(3, "Q", "quarterly", "BEA"),
    ]
    start, end, selected = propose_pilot_window(candidates, definition)
    assert start <= date(2026, 1, 1) <= end
    assert {item.frequency for item in selected} == {"weekly", "monthly", "quarterly"}
    assert len({item.source_family for item in selected}) == 3


def test_pilot_planner_rejects_nonrepresentative_inventory() -> None:
    with pytest.raises(ValueError, match="MISSING_REQUIRED_FREQUENCY"):
        propose_pilot_window([_candidate(1, "M")], load_validation_catalog().pilot_planner)


def test_materialization_plan_includes_discovery_snapshots_replay_and_lag() -> None:
    releases = EvaluationSchedule(
        cadence=ValidationCadence.RELEASE_EVENT_EOD,
        dates=[date(2026, 4, 30)],
        schedule_hash="release",
    )
    plan = build_pilot_plan(
        release_schedule=releases,
        window_start=date(2026, 4, 1),
        window_end=date(2026, 4, 30),
        indicator_ids=["A", "B"],
        trajectory_horizon_months=3,
    )
    assert date(2026, 1, 30) in plan.lag_endpoint_dates
    assert date(2026, 1, 30) not in plan.release_schedule.dates
    assert sum(item.unit_type == "VINTAGE_DISCOVERY" for item in plan.units) == 2
    assert sum(item.unit_type == "EXACT_PIT_SNAPSHOT" for item in plan.units) == 4
    assert plan.planned_logical_pit_units == len(plan.units)


def test_full_materialization_plan_is_not_authorized() -> None:
    releases = EvaluationSchedule(
        cadence=ValidationCadence.RELEASE_EVENT_EOD,
        dates=[date(2026, 1, 1)],
        schedule_hash="release",
    )
    with pytest.raises(ValueError, match="NOT_APPROVED"):
        build_pilot_plan(
            release_schedule=releases,
            window_start=date(2026, 1, 1),
            window_end=date(2026, 1, 31),
            indicator_ids=["A"],
            trajectory_horizon_months=3,
            phase=ValidationPhase.FULL,
        )
