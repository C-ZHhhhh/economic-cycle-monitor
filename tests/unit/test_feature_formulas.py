"""Pure offline coverage for Stage 3A formula and continuity contracts."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.features.formulas import (
    CALCULATION_PRECISION,
    FORMULA_REGISTRY,
    evaluate_formula,
    expected_input_dates,
    required_lookback,
    shift_period,
)
from app.features.schemas import FeatureStatus, FormulaId
from app.source_registry.schemas import EffectiveObservation, MappingQuality, RuntimeHealth


def _observation(
    observation_date: date,
    value: str,
    *,
    frequency: str = "monthly",
    unit: str = "index",
) -> EffectiveObservation:
    return EffectiveObservation(
        indicator_id="TEST_INDICATOR",
        observation_date=observation_date,
        value=Decimal(value),
        unit=unit,
        frequency=frequency,
        source_used="fred",
        source_identity="TEST",
        source_policy_version="stage2b5-v3",
        registry_hash="a" * 64,
        mapping_version="test-v1",
        is_fallback=False,
        resolution_reason="TEST",
        mapping_quality=MappingQuality.VALID,
        provider_health=RuntimeHealth.HEALTHY,
        indicator_source_health=RuntimeHealth.HEALTHY,
        release_date=observation_date,
        retrieved_at=datetime(2026, 8, 26, tzinfo=UTC),
        vintage_as_of=None,
        vintage_source=None,
        is_point_in_time_safe=False,
    )


def _series(
    target: date,
    frequency: str,
    values: list[str],
) -> dict[date, EffectiveObservation]:
    start = shift_period(target, frequency, -(len(values) - 1))
    return {
        shift_period(start, frequency, offset): _observation(
            shift_period(start, frequency, offset), value, frequency=frequency
        )
        for offset, value in enumerate(values)
    }


@pytest.mark.parametrize(
    ("formula_id", "window", "frequency", "values", "expected"),
    [
        (FormulaId.LEVEL, 1, "monthly", ["105"], "105"),
        (FormulaId.DIFF_K, 1, "monthly", ["100", "105"], "5"),
        (FormulaId.POINT_DIFF_K, 3, "monthly", ["4", "4.1", "4.2", "4.5"], "0.5"),
        (FormulaId.AVG_PERIOD_DIFF_K, 3, "monthly", ["100", "102", "106", "109"], "3"),
        (FormulaId.PCT_CHANGE_K, 1, "monthly", ["100", "105"], "5"),
        (FormulaId.MONTHLY_ANNUALIZED_K, 1, "monthly", ["100", "101"], "12.68250301"),
        (FormulaId.MONTHLY_ANNUALIZED_K, 3, "monthly", ["100", "101", "102", "103"], "12.550881"),
        (
            FormulaId.MONTHLY_ANNUALIZED_K,
            6,
            "monthly",
            ["100", "101", "102", "103", "104", "105", "106"],
            "12.36",
        ),
        (
            FormulaId.MONTHLY_YOY,
            12,
            "monthly",
            [str(100 + item) for item in range(13)],
            "12",
        ),
        (FormulaId.QUARTERLY_QOQ_ANNUALIZED, 1, "quarterly", ["100", "102"], "8.243216"),
        (
            FormulaId.QUARTERLY_YOY,
            4,
            "quarterly",
            ["100", "101", "102", "103", "108"],
            "8",
        ),
        (FormulaId.MOVING_AVERAGE_K, 4, "weekly", ["100", "110", "120", "130"], "115"),
        (
            FormulaId.MA3_BLOCK_GROWTH_ANNUALIZED,
            3,
            "monthly",
            ["100", "100", "100", "101", "101", "101"],
            "4.060401",
        ),
        (
            FormulaId.GROWTH_ACCEL_QOQ_1Q,
            1,
            "quarterly",
            ["100", "101", "103"],
            "4.0987834",
        ),
        (
            FormulaId.GROWTH_ACCEL_3M_NONOVERLAP,
            3,
            "monthly",
            ["100", "100.5", "101", "102", "102.5", "103", "104"],
            "-0.1663680",
        ),
        (
            FormulaId.MA4W_CHANGE_KW,
            4,
            "weekly",
            ["100", "100", "100", "100", "101", "101", "101", "101"],
            "1",
        ),
        (
            FormulaId.MA4W_CHANGE_KW,
            13,
            "weekly",
            ["100"] * 13 + ["102"] * 4,
            "2",
        ),
        (
            FormulaId.MA4W_YOY_PCT,
            52,
            "weekly",
            ["100"] * 52 + ["105"] * 4,
            "5",
        ),
    ],
)
def test_formula_registry_returns_expected_decimal_result(
    formula_id: FormulaId,
    window: int,
    frequency: str,
    values: list[str],
    expected: str,
) -> None:
    target = date(2024, 12, 1) if frequency != "weekly" else date(2024, 12, 28)
    outcome = evaluate_formula(
        formula_id,
        window=window,
        frequency=frequency,
        target=target,
        observations=_series(target, frequency, values),
    )
    assert outcome.status is FeatureStatus.OK
    assert outcome.value is not None
    tolerance = (
        Decimal("0.01")
        if formula_id
        in {
            FormulaId.GROWTH_ACCEL_QOQ_1Q,
            FormulaId.GROWTH_ACCEL_3M_NONOVERLAP,
        }
        else Decimal("0.0000001")
    )
    assert abs(outcome.value - Decimal(expected)) <= tolerance


@pytest.mark.parametrize(
    ("frequency", "target", "expected"),
    [
        ("weekly", date(2024, 1, 13), date(2024, 1, 6)),
        ("monthly", date(2024, 3, 1), date(2024, 2, 1)),
        ("quarterly", date(2024, 4, 1), date(2024, 1, 1)),
    ],
)
def test_native_period_shift_is_strict(frequency: str, target: date, expected: date) -> None:
    assert shift_period(target, frequency, -1) == expected


@pytest.mark.parametrize("frequency", ["weekly", "monthly", "quarterly"])
def test_missing_intermediate_period_is_not_skipped(frequency: str) -> None:
    target = date(2024, 3, 30) if frequency == "weekly" else date(2024, 7, 1)
    observations = _series(target, frequency, ["100", "101", "102", "103"])
    missing = shift_period(target, frequency, -1)
    observations.pop(missing)
    outcome = evaluate_formula(
        FormulaId.DIFF_K,
        window=3,
        frequency=frequency,
        target=target,
        observations=observations,
    )
    assert outcome.status is FeatureStatus.MISSING_INPUT_PERIOD
    assert outcome.value is None


def test_warmup_is_insufficient_history_not_missing_period() -> None:
    target = date(2024, 3, 1)
    observations = _series(target, "monthly", ["100", "101"])
    outcome = evaluate_formula(
        FormulaId.MONTHLY_YOY,
        window=12,
        frequency="monthly",
        target=target,
        observations=observations,
    )
    assert outcome.status is FeatureStatus.INSUFFICIENT_HISTORY


@pytest.mark.parametrize("denominator", ["0", "-1"])
def test_compound_growth_requires_positive_denominator(denominator: str) -> None:
    target = date(2024, 2, 1)
    outcome = evaluate_formula(
        FormulaId.MONTHLY_ANNUALIZED_K,
        window=1,
        frequency="monthly",
        target=target,
        observations=_series(target, "monthly", [denominator, "1"]),
    )
    assert outcome.status is FeatureStatus.INVALID_DENOMINATOR


def test_nonfinite_input_fails_closed() -> None:
    target = date(2024, 2, 1)
    observations = _series(target, "monthly", ["1", "1"])
    observations[date(2024, 1, 1)] = observations[date(2024, 1, 1)].model_copy(
        update={"value": Decimal("NaN")}
    )
    outcome = evaluate_formula(
        FormulaId.DIFF_K,
        window=1,
        frequency="monthly",
        target=target,
        observations=observations,
    )
    assert outcome.status is FeatureStatus.NONFINITE_INPUT


def test_decimal_overflow_is_reported_as_nonfinite_result() -> None:
    target = date(2024, 2, 1)
    outcome = evaluate_formula(
        FormulaId.MONTHLY_ANNUALIZED_K,
        window=1,
        frequency="monthly",
        target=target,
        observations=_series(target, "monthly", ["1", "1e999999"]),
    )
    assert outcome.status is FeatureStatus.NONFINITE_RESULT
    assert outcome.value is None


@pytest.mark.parametrize(
    ("formula_id", "frequency"),
    [
        (FormulaId.MONTHLY_YOY, "weekly"),
        (FormulaId.QUARTERLY_YOY, "monthly"),
        (FormulaId.MA4W_YOY_PCT, "quarterly"),
    ],
)
def test_frequency_incompatibility_fails_closed(formula_id: FormulaId, frequency: str) -> None:
    outcome = evaluate_formula(
        formula_id,
        window=1,
        frequency=frequency,
        target=date(2024, 1, 1),
        observations={},
    )
    assert outcome.status is FeatureStatus.UNSUPPORTED_TRANSFORM


def test_monthly_annualization_rejects_non_integer_exponent_window() -> None:
    target = date(2024, 5, 1)
    outcome = evaluate_formula(
        FormulaId.MONTHLY_ANNUALIZED_K,
        window=5,
        frequency="monthly",
        target=target,
        observations=_series(target, "monthly", ["100"] * 5 + ["101"]),
    )
    assert outcome.status is FeatureStatus.INVALID_DENOMINATOR


def test_formula_registry_is_closed_and_versioned_by_all_enum_members() -> None:
    assert set(FORMULA_REGISTRY) == set(FormulaId)
    assert CALCULATION_PRECISION == 34


@pytest.mark.parametrize(
    ("formula_id", "window", "lookback", "count"),
    [
        (FormulaId.LEVEL, 1, 0, 1),
        (FormulaId.MONTHLY_YOY, 12, 12, 13),
        (FormulaId.MA3_BLOCK_GROWTH_ANNUALIZED, 3, 5, 6),
        (FormulaId.GROWTH_ACCEL_3M_NONOVERLAP, 3, 6, 7),
        (FormulaId.MA4W_CHANGE_KW, 13, 16, 17),
        (FormulaId.MA4W_YOY_PCT, 52, 55, 56),
    ],
)
def test_minimum_history_and_expected_count_are_explicit(
    formula_id: FormulaId, window: int, lookback: int, count: int
) -> None:
    target = date(2024, 12, 1)
    assert required_lookback(formula_id, window) == lookback
    assert len(expected_input_dates(formula_id, target, "monthly", window)) == count


def test_weekly_dates_are_exactly_seven_days_apart() -> None:
    dates = expected_input_dates(FormulaId.MOVING_AVERAGE_K, date(2024, 2, 3), "weekly", 4)
    assert all(
        right - left == timedelta(days=7) for left, right in zip(dates, dates[1:], strict=False)
    )


def test_decimal_precision_does_not_quantize_intermediate_result() -> None:
    target = date(2024, 2, 1)
    outcome = evaluate_formula(
        FormulaId.MONTHLY_ANNUALIZED_K,
        window=1,
        frequency="monthly",
        target=target,
        observations=_series(target, "monthly", ["100", "100.1234567890123456789"]),
    )
    assert outcome.status is FeatureStatus.OK
    assert outcome.value is not None
    assert len(outcome.value.as_tuple().digits) > 20
