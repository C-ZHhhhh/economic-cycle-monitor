"""Stage 3F pure metrics, sensitivity, and hysteresis tests."""

from datetime import date
from decimal import Decimal

from app.validation.config import load_validation_catalog
from app.validation.metrics import (
    calendar_duration_occupancy,
    event_count_occupancy,
    regime_durations,
    transition_counts,
)
from app.validation.schemas import ExactRatio, RegimeHistoryRow, UniverseMode, ValidationCadence
from app.validation.sensitivity import (
    apply_hysteresis,
    counterfactual_level,
    counterfactual_trajectory,
    hysteresis_metrics,
    limited_revision_pairs,
)


def _row(
    day: int,
    *,
    regime: str | None = "A",
    growth: str = "WEAK",
    inflation: str = "LOW_PRESSURE",
    growth_trajectory: str = "STABLE",
    inflation_trajectory: str = "STABLE",
    growth_value: str = "-0.2",
    inflation_value: str = "-0.2",
    confidence: str = "1",
) -> RegimeHistoryRow:
    return RegimeHistoryRow(
        universe_mode=UniverseMode.FULL_FIXED_UNIVERSE,
        cadence=ValidationCadence.RELEASE_EVENT_EOD,
        evaluation_as_of=date(2026, 1, day),
        growth_strength=Decimal(growth_value),
        inflation_pressure=Decimal(inflation_value),
        growth_trajectory_delta=Decimal("0"),
        inflation_trajectory_delta=Decimal("0"),
        base_regime_id=regime,
        growth_condition=growth,
        inflation_condition=inflation,
        growth_trajectory=growth_trajectory,
        inflation_trajectory=inflation_trajectory,
        minimum_axis_data_confidence=Decimal(confidence),
        regime_status="OK",
        is_point_in_time_safe=True,
        regime_calculation_hash=f"regime-{day}",
        macro_state_calculation_hash=f"state-{day}",
        semantic_validation_status="FULL",
    )


def test_base_transition_requires_base_regime_id_change() -> None:
    rows = [
        _row(1),
        _row(2, growth_trajectory="IMPROVING"),
        _row(3, regime="B", growth="STRONG", growth_trajectory="IMPROVING"),
    ]
    counts = transition_counts(rows)
    assert counts["base_regime"] == 1
    assert counts["growth_trajectory"] == 1
    assert counts["growth_condition"] == 1


def test_macro_hash_or_confidence_change_is_not_a_base_transition() -> None:
    second = _row(2, confidence="0.8").model_copy(
        update={"macro_state_calculation_hash": "revised-state"}
    )
    assert transition_counts([_row(1), second])["base_regime"] == 0


def test_event_count_and_calendar_duration_occupancy_are_distinct() -> None:
    rows = [_row(1, regime="A"), _row(2, regime="B"), _row(10, regime="B")]
    event = event_count_occupancy(rows)
    duration = calendar_duration_occupancy(rows, through=date(2026, 1, 11))
    assert event["A"] == Decimal(1) / Decimal(3)
    assert duration["A"] == Decimal(1) / Decimal(10)
    assert duration["B"] == Decimal(9) / Decimal(10)


def test_regime_duration_tracks_events_and_calendar_days() -> None:
    durations = regime_durations([_row(1, regime="A"), _row(3, regime="A"), _row(4, regime="B")])
    assert durations[0]["evaluation_count"] == 2
    assert durations[0]["calendar_days"] == 2
    assert durations[1]["base_regime_id"] == "B"


def test_production_sensitivity_values_are_present_in_grid() -> None:
    sensitivity = load_validation_catalog().sensitivity
    assert sensitivity.production.neutral_band in sensitivity.neutral_bands
    assert (
        sensitivity.production.trajectory_horizon_months in sensitivity.trajectory_horizons_months
    )
    assert sensitivity.production.trajectory_stable_band in sensitivity.trajectory_stable_bands
    assert any(
        item.numerator == 2 and item.denominator == 3 for item in sensitivity.confidence_gates
    )


def test_counterfactual_level_uses_exact_confidence_boundary() -> None:
    row = _row(1, growth_value="0.11", inflation_value="0", confidence=str(Decimal(2) / 3))
    result = counterfactual_level(
        row, neutral_band=Decimal("0.10"), confidence_gate=ExactRatio(numerator=2, denominator=3)
    )
    assert result["growth_condition"] == "STRONG"
    assert result["inflation_condition"] == "NEUTRAL_PRESSURE"
    assert result["base_regime_id"] is not None


def test_counterfactual_level_fails_closed_below_confidence() -> None:
    row = _row(1, confidence="0.66")
    result = counterfactual_level(
        row, neutral_band=Decimal("0.10"), confidence_gate=ExactRatio(numerator=2, denominator=3)
    )
    assert result["base_regime_id"] is None


def test_counterfactual_trajectory_requires_exact_calendar_lag_endpoint() -> None:
    january = _row(1, growth_value="0", inflation_value="0")
    april = _row(4, growth_value="0.5", inflation_value="-0.5").model_copy(
        update={"evaluation_as_of": date(2026, 4, 1)}
    )
    results = counterfactual_trajectory(
        [january, april], horizon_months=3, stable_band=Decimal("0.10")
    )
    assert results[1]["lag_as_of"] == date(2026, 1, 1)
    assert results[1]["growth_trajectory"] == "IMPROVING"
    assert results[1]["inflation_trajectory"] == "FALLING"


def test_counterfactual_trajectory_does_not_use_nearest_schedule_event() -> None:
    current = _row(2).model_copy(update={"evaluation_as_of": date(2026, 4, 2)})
    results = counterfactual_trajectory(
        [_row(1), current], horizon_months=3, stable_band=Decimal("0.10")
    )
    assert results[1]["lag_as_of"] == date(2026, 1, 2)
    assert results[1]["growth_trajectory"] is None


def test_hysteresis_policies_do_not_mutate_input_history() -> None:
    original = ["A", "B", "B", "A"]
    assert apply_hysteresis(original, "NONE") == original
    assert apply_hysteresis(original, "2_CONSECUTIVE") == ["A", "A", "B", "B"]
    assert original == ["A", "B", "B", "A"]


def test_hysteresis_reports_transitions_whipsaw_and_occupancy_without_ranking() -> None:
    metrics = hysteresis_metrics(
        [date(2026, 1, day) for day in range(1, 5)],
        ["A", "B", "A", "A"],
        "NONE",
    )
    assert metrics["transition_count"] == 2
    assert metrics["whipsaw_count"] == 1
    assert "rank" not in metrics


def test_revision_sensitivity_is_limited_to_approved_pairs() -> None:
    evidence: list[dict[str, object]] = [
        {"indicator_id": "US_REAL_GDP", "first_known_hash": "a", "later_vintage_hash": "b"},
        {"indicator_id": "US_CPI", "first_known_hash": "c", "later_vintage_hash": "d"},
        {"indicator_id": "US_HOUSING_STARTS", "first_known_hash": "e", "later_vintage_hash": "f"},
    ]
    selected = limited_revision_pairs(
        evidence,
        permitted_indicators=set(load_validation_catalog().revision_sensitivity.indicators),
    )
    assert [item["indicator_id"] for item in selected] == ["US_REAL_GDP", "US_CPI"]
    assert all("history" not in item for item in selected)


def test_deferred_stage3c_and_stage3d_sensitivities_are_not_implemented_as_grids() -> None:
    catalog = load_validation_catalog()
    assert set(catalog.sensitivity.deferred) == {
        "STAGE3C_TANH_SCALE",
        "STAGE3C_PILLAR_COVERAGE",
        "STAGE3D_GROWTH_QUORUM",
    }
