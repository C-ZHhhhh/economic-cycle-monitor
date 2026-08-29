"""Deterministic Decimal calculations for Stage 3E."""

from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from app.regimes.schemas import (
    GrowthCondition,
    GrowthTrajectoryQualifier,
    InflationCondition,
    InflationTrajectoryQualifier,
)


def subtract_calendar_months(value: date, months: int) -> date:
    """Subtract calendar months while preserving month-end semantics."""
    if months < 1:
        raise ValueError("months must be positive")
    return value - relativedelta(months=months)


def classify_growth(value: Decimal, *, lower: Decimal, upper: Decimal) -> GrowthCondition:
    """Classify Growth with exact thresholds included in the neutral band."""
    if value < lower:
        return GrowthCondition.WEAK
    if value > upper:
        return GrowthCondition.STRONG
    return GrowthCondition.NEUTRAL


def classify_inflation(value: Decimal, *, lower: Decimal, upper: Decimal) -> InflationCondition:
    """Classify Inflation Pressure without assigning welfare meaning."""
    if value < lower:
        return InflationCondition.LOW_PRESSURE
    if value > upper:
        return InflationCondition.HIGH_PRESSURE
    return InflationCondition.NEUTRAL_PRESSURE


def classification_margin(value: Decimal, *, lower: Decimal, upper: Decimal) -> Decimal:
    """Return distance to the nearest level-classification boundary."""
    return min(abs(value - lower), abs(value - upper))


def classify_growth_trajectory(
    delta: Decimal, *, lower: Decimal, upper: Decimal
) -> GrowthTrajectoryQualifier:
    """Classify a Growth delta with exact boundaries included as stable."""
    if delta < lower:
        return GrowthTrajectoryQualifier.DETERIORATING
    if delta > upper:
        return GrowthTrajectoryQualifier.IMPROVING
    return GrowthTrajectoryQualifier.STABLE


def classify_inflation_trajectory(
    delta: Decimal, *, lower: Decimal, upper: Decimal
) -> InflationTrajectoryQualifier:
    """Classify an Inflation Pressure delta without good/bad semantics."""
    if delta < lower:
        return InflationTrajectoryQualifier.FALLING
    if delta > upper:
        return InflationTrajectoryQualifier.RISING
    return InflationTrajectoryQualifier.STABLE
