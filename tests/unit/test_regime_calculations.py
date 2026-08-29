"""Exact Stage 3E level, trajectory, date, and margin calculations."""

from datetime import date
from decimal import Decimal

import pytest

from app.regimes.calculations import (
    classification_margin,
    classify_growth,
    classify_growth_trajectory,
    classify_inflation,
    classify_inflation_trajectory,
    subtract_calendar_months,
)
from app.regimes.schemas import (
    GrowthCondition,
    GrowthTrajectoryQualifier,
    InflationCondition,
    InflationTrajectoryQualifier,
)

LOWER = Decimal("-0.10")
UPPER = Decimal("0.10")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("-0.1000001"), GrowthCondition.WEAK),
        (LOWER, GrowthCondition.NEUTRAL),
        (Decimal("0"), GrowthCondition.NEUTRAL),
        (UPPER, GrowthCondition.NEUTRAL),
        (Decimal("0.1000001"), GrowthCondition.STRONG),
    ],
)
def test_growth_level_boundaries(value: Decimal, expected: GrowthCondition) -> None:
    assert classify_growth(value, lower=LOWER, upper=UPPER) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("-0.1000001"), InflationCondition.LOW_PRESSURE),
        (LOWER, InflationCondition.NEUTRAL_PRESSURE),
        (Decimal("0"), InflationCondition.NEUTRAL_PRESSURE),
        (UPPER, InflationCondition.NEUTRAL_PRESSURE),
        (Decimal("0.1000001"), InflationCondition.HIGH_PRESSURE),
    ],
)
def test_inflation_level_boundaries(value: Decimal, expected: InflationCondition) -> None:
    assert classify_inflation(value, lower=LOWER, upper=UPPER) is expected


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (Decimal("-0.1000001"), GrowthTrajectoryQualifier.DETERIORATING),
        (LOWER, GrowthTrajectoryQualifier.STABLE),
        (Decimal("0"), GrowthTrajectoryQualifier.STABLE),
        (UPPER, GrowthTrajectoryQualifier.STABLE),
        (Decimal("0.1000001"), GrowthTrajectoryQualifier.IMPROVING),
    ],
)
def test_growth_trajectory_boundaries(delta: Decimal, expected: GrowthTrajectoryQualifier) -> None:
    assert classify_growth_trajectory(delta, lower=LOWER, upper=UPPER) is expected


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (Decimal("-0.1000001"), InflationTrajectoryQualifier.FALLING),
        (LOWER, InflationTrajectoryQualifier.STABLE),
        (Decimal("0"), InflationTrajectoryQualifier.STABLE),
        (UPPER, InflationTrajectoryQualifier.STABLE),
        (Decimal("0.1000001"), InflationTrajectoryQualifier.RISING),
    ],
)
def test_inflation_trajectory_boundaries(
    delta: Decimal, expected: InflationTrajectoryQualifier
) -> None:
    assert classify_inflation_trajectory(delta, lower=LOWER, upper=UPPER) is expected


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (date(2026, 8, 29), date(2026, 5, 29)),
        (date(2026, 5, 31), date(2026, 2, 28)),
        (date(2024, 5, 31), date(2024, 2, 29)),
        (date(2024, 2, 29), date(2023, 11, 29)),
    ],
)
def test_three_calendar_month_subtraction(current: date, expected: date) -> None:
    assert subtract_calendar_months(current, 3) == expected


def test_month_subtraction_rejects_nonpositive_horizon() -> None:
    with pytest.raises(ValueError, match="positive"):
        subtract_calendar_months(date(2026, 8, 29), 0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("-0.15"), Decimal("0.05")),
        (Decimal("-0.05"), Decimal("0.05")),
        (Decimal("0"), Decimal("0.10")),
        (Decimal("0.05"), Decimal("0.05")),
        (Decimal("0.1500001"), Decimal("0.0500001")),
    ],
)
def test_classification_margin_is_nearest_boundary(value: Decimal, expected: Decimal) -> None:
    assert classification_margin(value, lower=LOWER, upper=UPPER) == expected
