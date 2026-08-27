"""Typed, period-aware Stage 3A formula registry."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, DecimalException, InvalidOperation, localcontext

from dateutil.relativedelta import relativedelta

from app.features.schemas import FeatureStatus, FormulaId
from app.source_registry.schemas import EffectiveObservation

FORMULA_REGISTRY_VERSION = "stage3a-formulas-v1"
CALCULATION_PRECISION = 34


@dataclass(frozen=True)
class FormulaContract:
    """Static compatibility and lookback metadata for one formula."""

    formula_id: FormulaId
    frequencies: frozenset[str]
    positive_denominator: bool = False


@dataclass(frozen=True)
class FormulaOutcome:
    """Raw formula result plus exact observations used."""

    value: Decimal | None
    status: FeatureStatus
    expected_dates: tuple[date, ...]
    inputs: tuple[EffectiveObservation, ...]


ALL_FREQUENCIES = frozenset({"weekly", "monthly", "quarterly"})
FORMULA_REGISTRY: dict[FormulaId, FormulaContract] = {
    FormulaId.LEVEL: FormulaContract(FormulaId.LEVEL, ALL_FREQUENCIES),
    FormulaId.DIFF_K: FormulaContract(FormulaId.DIFF_K, ALL_FREQUENCIES),
    FormulaId.POINT_DIFF_K: FormulaContract(FormulaId.POINT_DIFF_K, ALL_FREQUENCIES),
    FormulaId.AVG_PERIOD_DIFF_K: FormulaContract(FormulaId.AVG_PERIOD_DIFF_K, ALL_FREQUENCIES),
    FormulaId.PCT_CHANGE_K: FormulaContract(FormulaId.PCT_CHANGE_K, ALL_FREQUENCIES, True),
    FormulaId.MONTHLY_ANNUALIZED_K: FormulaContract(
        FormulaId.MONTHLY_ANNUALIZED_K, frozenset({"monthly"}), True
    ),
    FormulaId.MONTHLY_YOY: FormulaContract(FormulaId.MONTHLY_YOY, frozenset({"monthly"}), True),
    FormulaId.QUARTERLY_QOQ_ANNUALIZED: FormulaContract(
        FormulaId.QUARTERLY_QOQ_ANNUALIZED, frozenset({"quarterly"}), True
    ),
    FormulaId.QUARTERLY_YOY: FormulaContract(
        FormulaId.QUARTERLY_YOY, frozenset({"quarterly"}), True
    ),
    FormulaId.MOVING_AVERAGE_K: FormulaContract(FormulaId.MOVING_AVERAGE_K, ALL_FREQUENCIES),
    FormulaId.MA3_BLOCK_GROWTH_ANNUALIZED: FormulaContract(
        FormulaId.MA3_BLOCK_GROWTH_ANNUALIZED, frozenset({"monthly"}), True
    ),
    FormulaId.GROWTH_ACCEL_QOQ_1Q: FormulaContract(
        FormulaId.GROWTH_ACCEL_QOQ_1Q, frozenset({"quarterly"}), True
    ),
    FormulaId.GROWTH_ACCEL_3M_NONOVERLAP: FormulaContract(
        FormulaId.GROWTH_ACCEL_3M_NONOVERLAP, frozenset({"monthly"}), True
    ),
    FormulaId.MA4W_CHANGE_KW: FormulaContract(FormulaId.MA4W_CHANGE_KW, frozenset({"weekly"})),
    FormulaId.MA4W_YOY_PCT: FormulaContract(FormulaId.MA4W_YOY_PCT, frozenset({"weekly"}), True),
}


def shift_period(value: date, frequency: str, periods: int) -> date:
    """Shift a native observation date without inventing a resampled period."""
    if frequency == "weekly":
        return value + timedelta(days=7 * periods)
    if frequency == "monthly":
        return value + relativedelta(months=periods)
    if frequency == "quarterly":
        return value + relativedelta(months=3 * periods)
    raise ValueError(f"Unsupported frequency: {frequency}")


def required_lookback(formula_id: FormulaId, window: int) -> int:
    """Return native periods needed before a target feature date."""
    if formula_id is FormulaId.LEVEL:
        return 0
    if formula_id in {
        FormulaId.DIFF_K,
        FormulaId.POINT_DIFF_K,
        FormulaId.AVG_PERIOD_DIFF_K,
        FormulaId.PCT_CHANGE_K,
        FormulaId.MONTHLY_ANNUALIZED_K,
    }:
        return window
    if formula_id is FormulaId.MONTHLY_YOY:
        return 12
    if formula_id is FormulaId.QUARTERLY_QOQ_ANNUALIZED:
        return 1
    if formula_id is FormulaId.QUARTERLY_YOY:
        return 4
    if formula_id is FormulaId.MOVING_AVERAGE_K:
        return window - 1
    if formula_id is FormulaId.MA3_BLOCK_GROWTH_ANNUALIZED:
        return 5
    if formula_id is FormulaId.GROWTH_ACCEL_QOQ_1Q:
        return 2
    if formula_id is FormulaId.GROWTH_ACCEL_3M_NONOVERLAP:
        return 6
    if formula_id is FormulaId.MA4W_CHANGE_KW:
        return window + 3
    if formula_id is FormulaId.MA4W_YOY_PCT:
        return 55
    raise ValueError(f"Unsupported formula: {formula_id}")


def expected_input_dates(
    formula_id: FormulaId, target: date, frequency: str, window: int
) -> tuple[date, ...]:
    """Enumerate every native period required, including continuity checks."""
    lookback = required_lookback(formula_id, window)
    return tuple(shift_period(target, frequency, offset) for offset in range(-lookback, 1))


def _annualized(current: Decimal, previous: Decimal, exponent: int) -> Decimal:
    if previous <= 0 or current < 0:
        raise InvalidOperation
    return Decimal(100) * ((current / previous) ** exponent - Decimal(1))


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _calculate(formula_id: FormulaId, values: list[Decimal], window: int) -> Decimal:
    current = values[-1]
    previous = values[0]
    if formula_id is FormulaId.LEVEL:
        return current
    if formula_id in {FormulaId.DIFF_K, FormulaId.POINT_DIFF_K}:
        return current - previous
    if formula_id is FormulaId.AVG_PERIOD_DIFF_K:
        return (current - previous) / Decimal(window)
    if formula_id is FormulaId.PCT_CHANGE_K:
        return Decimal(100) * (current / previous - Decimal(1))
    if formula_id is FormulaId.MONTHLY_ANNUALIZED_K:
        if 12 % window:
            raise ValueError("MONTHLY_ANNUALIZED_K requires a divisor of 12")
        return _annualized(current, previous, 12 // window)
    if formula_id is FormulaId.MONTHLY_YOY:
        return Decimal(100) * (current / previous - Decimal(1))
    if formula_id is FormulaId.QUARTERLY_QOQ_ANNUALIZED:
        return _annualized(current, previous, 4)
    if formula_id is FormulaId.QUARTERLY_YOY:
        return Decimal(100) * (current / previous - Decimal(1))
    if formula_id is FormulaId.MOVING_AVERAGE_K:
        return _mean(values)
    if formula_id is FormulaId.MA3_BLOCK_GROWTH_ANNUALIZED:
        prior = _mean(values[:3])
        recent = _mean(values[3:])
        return _annualized(recent, prior, 4)
    if formula_id is FormulaId.GROWTH_ACCEL_QOQ_1Q:
        previous_growth = _annualized(values[1], values[0], 4)
        current_growth = _annualized(values[2], values[1], 4)
        return current_growth - previous_growth
    if formula_id is FormulaId.GROWTH_ACCEL_3M_NONOVERLAP:
        previous_growth = _annualized(values[3], values[0], 4)
        current_growth = _annualized(values[6], values[3], 4)
        return current_growth - previous_growth
    if formula_id is FormulaId.MA4W_CHANGE_KW:
        return _mean(values[-4:]) - _mean(values[:4])
    if formula_id is FormulaId.MA4W_YOY_PCT:
        prior = _mean(values[:4])
        recent = _mean(values[-4:])
        return Decimal(100) * (recent / prior - Decimal(1))
    raise ValueError(f"Unsupported formula: {formula_id}")


def evaluate_formula(
    formula_id: FormulaId,
    *,
    window: int,
    frequency: str,
    target: date,
    observations: dict[date, EffectiveObservation],
) -> FormulaOutcome:
    """Evaluate one formula with strict native-period continuity and Decimal math."""
    contract = FORMULA_REGISTRY.get(formula_id)
    if contract is None or frequency not in contract.frequencies:
        return FormulaOutcome(None, FeatureStatus.UNSUPPORTED_TRANSFORM, (), ())
    expected = expected_input_dates(formula_id, target, frequency, window)
    available = tuple(observations[item] for item in expected if item in observations)
    if len(available) != len(expected):
        earliest = min(observations) if observations else None
        status = (
            FeatureStatus.INSUFFICIENT_HISTORY
            if earliest is None or expected[0] < earliest
            else FeatureStatus.MISSING_INPUT_PERIOD
        )
        return FormulaOutcome(None, status, expected, available)
    values = [Decimal(item.value) for item in available]
    if any(not item.is_finite() for item in values):
        return FormulaOutcome(None, FeatureStatus.NONFINITE_INPUT, expected, available)
    if contract.positive_denominator and values[0] <= 0:
        return FormulaOutcome(None, FeatureStatus.INVALID_DENOMINATOR, expected, available)
    try:
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            result = _calculate(formula_id, values, window)
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return FormulaOutcome(None, FeatureStatus.INVALID_DENOMINATOR, expected, available)
    except DecimalException:
        return FormulaOutcome(None, FeatureStatus.NONFINITE_RESULT, expected, available)
    if not result.is_finite():
        return FormulaOutcome(None, FeatureStatus.NONFINITE_RESULT, expected, available)
    return FormulaOutcome(result, FeatureStatus.OK, expected, available)
