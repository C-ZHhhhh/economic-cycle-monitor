"""Hard budget and checkpoint/resume tests."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.validation.budget import BudgetStop, BudgetTracker
from app.validation.checkpoint import CheckpointMismatch, load_checkpoint, write_checkpoint
from app.validation.schemas import (
    BudgetLimits,
    Checkpoint,
    StopReason,
    UniverseMode,
)


def _limits(**updates: int) -> BudgetLimits:
    values = {
        "external_alfred_http_requests": 2000,
        "incremental_storage_bytes": 1000,
        "wall_clock_seconds": 100,
        "alfred_concurrency": 1,
        "global_http_concurrency": 4,
        "per_source_http_concurrency": 2,
    }
    values.update(updates)
    return BudgetLimits(**values)


def test_external_http_attempt_budget_counts_retries() -> None:
    tracker = BudgetTracker(_limits(), checkpoint=lambda _reason: None)
    tracker.before_http_attempt(attempt_number=1)
    tracker.before_http_attempt(attempt_number=2)
    tracker.before_http_attempt(attempt_number=3)
    assert tracker.external_alfred_http_requests == 3
    assert tracker.retry_count == 2


def test_request_2001_never_executes_and_checkpoint_precedes_stop() -> None:
    order: list[str] = []
    tracker = BudgetTracker(
        _limits(), checkpoint=lambda reason: order.append(f"checkpoint:{reason.value}")
    )
    for _ in range(2000):
        tracker.before_http_attempt(attempt_number=1)
    with pytest.raises(BudgetStop, match="REQUEST_BUDGET"):
        tracker.before_http_attempt(attempt_number=1)
    order.append("stopped")
    assert tracker.external_alfred_http_requests == 2000
    assert order == ["checkpoint:REQUEST_BUDGET", "stopped"]


def test_logical_units_are_independent_from_http_attempts() -> None:
    tracker = BudgetTracker(_limits(), checkpoint=lambda _reason: None)
    tracker.begin_logical_unit()
    tracker.complete_logical_unit(reused=True)
    assert tracker.logical_pit_units == 1
    assert tracker.completed_logical_units == 1
    assert tracker.cache_reuse_count == 1
    assert tracker.external_alfred_http_requests == 0


def test_storage_cap_checkpoints_before_stop() -> None:
    reasons: list[StopReason] = []
    tracker = BudgetTracker(_limits(incremental_storage_bytes=10), checkpoint=reasons.append)
    with pytest.raises(BudgetStop) as captured:
        tracker.update_storage(10)
    assert captured.value.reason is StopReason.STORAGE_BUDGET
    assert reasons == [StopReason.STORAGE_BUDGET]


def test_wall_clock_cap_checkpoints_before_stop() -> None:
    readings = iter([0.0, 10.0])
    reasons: list[StopReason] = []
    tracker = BudgetTracker(
        _limits(wall_clock_seconds=10),
        checkpoint=reasons.append,
        monotonic=lambda: next(readings),
    )
    with pytest.raises(BudgetStop) as captured:
        tracker.check_time()
    assert captured.value.reason is StopReason.WALL_CLOCK_BUDGET
    assert reasons == [StopReason.WALL_CLOCK_BUDGET]


def _checkpoint() -> Checkpoint:
    return Checkpoint(
        validation_spec_hash="validation",
        production_authority_hashes=["a", "b"],
        schedule_hash="schedule",
        universe_mode=UniverseMode.FULL_FIXED_UNIVERSE,
        completed_logical_units=["unit-1"],
        failed_units={"unit-2": "OUTSIDE_COVERAGE"},
        counters={
            "logical_pit_units": 2,
            "completed_logical_units": 1,
            "external_alfred_http_requests": 1,
            "retry_count": 0,
            "cache_reuse_count": 1,
            "incremental_storage_bytes": 100,
            "elapsed_wall_seconds": "1.5",
        },
        artifact_hashes={"manifest": "hash"},
        stop_reason=StopReason.REQUEST_BUDGET,
    )


def test_checkpoint_round_trip_is_atomic_and_typed(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "checkpoint.json"
    write_checkpoint(path, _checkpoint())
    loaded = load_checkpoint(
        path,
        validation_spec_hash="validation",
        production_authority_hashes=["a", "b"],
        schedule_hash="schedule",
    )
    assert loaded is not None
    assert loaded.completed_logical_units == ["unit-1"]
    assert not (path.parent / ".checkpoint.json.tmp").exists()


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"validation_spec_hash": "wrong"}, "VALIDATION_AUTHORITY"),
        ({"production_authority_hashes": ["wrong"]}, "PRODUCTION_AUTHORITY"),
        ({"schedule_hash": "wrong"}, "SCHEDULE"),
    ],
)
def test_checkpoint_authority_or_schedule_change_fails_closed(
    tmp_path: Path, kwargs: dict[str, object], match: str
) -> None:
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, _checkpoint())
    expected: dict[str, object] = {
        "validation_spec_hash": "validation",
        "production_authority_hashes": ["a", "b"],
        "schedule_hash": "schedule",
    }
    expected.update(kwargs)
    with pytest.raises(CheckpointMismatch, match=match):
        load_checkpoint(path, **expected)  # type: ignore[arg-type]


def test_missing_checkpoint_is_a_clean_start(tmp_path: Path) -> None:
    assert (
        load_checkpoint(
            tmp_path / "missing.json",
            validation_spec_hash="v",
            production_authority_hashes=[],
            schedule_hash="s",
        )
        is None
    )


def test_checkpoint_timestamp_is_not_a_semantic_unit_field() -> None:
    checkpoint = _checkpoint()
    assert checkpoint.checkpoint_timestamp.date() >= datetime.now(UTC).date()
    assert "checkpoint_timestamp" not in type(checkpoint.counters).model_fields
