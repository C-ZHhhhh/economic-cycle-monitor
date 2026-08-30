"""Coverage-boundary proof and deterministic replay tests."""

from datetime import date
from decimal import Decimal

from app.validation.config import load_validation_catalog
from app.validation.coverage import boundary_statuses, discover_boundaries, full_primary_date
from app.validation.replay import history_run_hash
from app.validation.schemas import (
    BoundaryCandidateStatus,
    CoverageBoundary,
    CoverageSnapshot,
    RegimeHistoryRow,
    UniverseMode,
    ValidationCadence,
)


def _coverage(day: int, count: int) -> CoverageSnapshot:
    full = count == 33
    return CoverageSnapshot(
        evaluation_as_of=date(2026, 1, day),
        raw_available_count=count,
        feature_available_count=count,
        signal_available_count=count,
        pillar_available_count=9 if full else 8,
        macro_state_available=full,
        regime_level_available=full,
        regime_trajectory_available=full,
        calculation_hashes=[f"hash-{day}"],
        source_evidence=[f"source-{day}"],
    )


def _history(day: int, regime: str = "R") -> RegimeHistoryRow:
    return RegimeHistoryRow(
        universe_mode=UniverseMode.FULL_FIXED_UNIVERSE,
        cadence=ValidationCadence.RELEASE_EVENT_EOD,
        evaluation_as_of=date(2026, 1, day),
        growth_strength=Decimal("0.2"),
        inflation_pressure=Decimal("0.1"),
        growth_trajectory_delta=Decimal("0.01"),
        inflation_trajectory_delta=Decimal("0.02"),
        base_regime_id=regime,
        growth_condition="STRONG",
        inflation_condition="NEUTRAL_PRESSURE",
        growth_trajectory="STABLE",
        inflation_trajectory="STABLE",
        minimum_axis_data_confidence=Decimal(1),
        regime_status="OK",
        is_point_in_time_safe=True,
        regime_calculation_hash=f"regime-{day}",
        macro_state_calculation_hash=f"state-{day}",
        semantic_validation_status="FULL",
    )


def test_boundary_first_success_preserves_previous_candidate_evidence() -> None:
    discovered, unresolved = discover_boundaries(
        [_coverage(1, 32), _coverage(2, 33)], authority_hashes=["authority"]
    )
    signal = next(item for item in discovered if item.boundary is CoverageBoundary.SIGNAL_PIT_START)
    assert signal.first_successful_candidate_date == date(2026, 1, 2)
    assert signal.previous_candidate_date == date(2026, 1, 1)
    assert signal.previous_candidate_status is BoundaryCandidateStatus.PARTIAL
    assert not unresolved


def test_boundary_without_success_remains_unresolved() -> None:
    discovered, unresolved = discover_boundaries([_coverage(1, 20)], authority_hashes=["authority"])
    assert not discovered
    assert set(unresolved) == set(CoverageBoundary)


def test_layer_boundaries_are_not_inferred_from_theoretical_date() -> None:
    snapshot = _coverage(15, 32)
    assert date(2026, 1, 15) == snapshot.evaluation_as_of
    assert not any(boundary_statuses(snapshot).values())


def test_downstream_boundaries_require_each_upstream_layer() -> None:
    snapshot = _coverage(15, 33).model_copy(
        update={
            "macro_state_available": False,
            "regime_level_available": True,
            "regime_trajectory_available": True,
        }
    )
    statuses = boundary_statuses(snapshot)
    assert statuses[CoverageBoundary.PILLAR_PIT_START]
    assert not statuses[CoverageBoundary.MACRO_STATE_PIT_START]
    assert not statuses[CoverageBoundary.REGIME_LEVEL_PIT_START]
    assert not statuses[CoverageBoundary.REGIME_TRAJECTORY_PIT_START]


def test_reported_boundary_aliases_map_to_evidence() -> None:
    snapshots = [_coverage(1, 32), _coverage(2, 33)]
    assert full_primary_date(snapshots, "earliest_common_full_primary_pit_date") == date(2026, 1, 2)
    assert full_primary_date(snapshots, "earliest_full_pillar_state_date") == date(2026, 1, 2)
    assert full_primary_date(snapshots, "earliest_full_regime_level_date") == date(2026, 1, 2)
    assert full_primary_date(snapshots, "earliest_full_regime_trajectory_date") == date(2026, 1, 2)


def test_history_hash_is_order_deterministic() -> None:
    catalog = load_validation_catalog()
    first = history_run_hash(
        validation_spec_hash=catalog.specification_hash,
        authorities=catalog.authorities,
        universe_mode=UniverseMode.FULL_FIXED_UNIVERSE,
        schedule_hash="schedule",
        rows=[_history(1), _history(2)],
    )
    second = history_run_hash(
        validation_spec_hash=catalog.specification_hash,
        authorities=catalog.authorities,
        universe_mode=UniverseMode.FULL_FIXED_UNIVERSE,
        schedule_hash="schedule",
        rows=[_history(2), _history(1)],
    )
    assert first == second


def test_history_hash_changes_with_semantic_regime_result() -> None:
    catalog = load_validation_catalog()
    base = history_run_hash(
        validation_spec_hash=catalog.specification_hash,
        authorities=catalog.authorities,
        universe_mode=UniverseMode.FULL_FIXED_UNIVERSE,
        schedule_hash="schedule",
        rows=[_history(1)],
    )
    changed = _history(1, regime="OTHER").model_copy(update={"regime_calculation_hash": "changed"})
    revised = history_run_hash(
        validation_spec_hash=catalog.specification_hash,
        authorities=catalog.authorities,
        universe_mode=UniverseMode.FULL_FIXED_UNIVERSE,
        schedule_hash="schedule",
        rows=[changed],
    )
    assert base != revised


def test_history_hash_binds_universe_and_schedule() -> None:
    catalog = load_validation_catalog()
    fixed = history_run_hash(
        validation_spec_hash=catalog.specification_hash,
        authorities=catalog.authorities,
        universe_mode=UniverseMode.FULL_FIXED_UNIVERSE,
        schedule_hash="release",
        rows=[_history(1)],
    )
    dynamic = history_run_hash(
        validation_spec_hash=catalog.specification_hash,
        authorities=catalog.authorities,
        universe_mode=UniverseMode.DYNAMIC_REAL_TIME_UNIVERSE,
        schedule_hash="release",
        rows=[_history(1)],
    )
    month_end = history_run_hash(
        validation_spec_hash=catalog.specification_hash,
        authorities=catalog.authorities,
        universe_mode=UniverseMode.FULL_FIXED_UNIVERSE,
        schedule_hash="month-end",
        rows=[_history(1)],
    )
    assert len({fixed, dynamic, month_end}) == 3
