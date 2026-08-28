"""Deterministic Decimal calculations for Stage 3D Growth state."""

from collections.abc import Mapping
from decimal import Decimal, localcontext


def _validate_inventory(
    values: Mapping[str, Decimal | None], base_weights: Mapping[str, Decimal]
) -> None:
    if set(values) != set(base_weights):
        raise ValueError("Growth value and weight inventories must match")
    if not values:
        raise ValueError("Growth inventory cannot be empty")
    if any(weight <= 0 or not weight.is_finite() for weight in base_weights.values()):
        raise ValueError("Growth base weights must be finite and positive")
    if sum(base_weights.values(), Decimal(0)) != Decimal(1):
        raise ValueError("Growth base weights must sum to one")
    for value in values.values():
        if value is not None and (not value.is_finite() or not Decimal(-1) < value < Decimal(1)):
            raise ValueError("Growth pillar values must be finite and strictly bounded")


def conditional_weighted_mean(
    values: Mapping[str, Decimal | None],
    base_weights: Mapping[str, Decimal],
    *,
    minimum_valid: int,
) -> tuple[Decimal | None, dict[str, Decimal], Decimal]:
    """Return score, conditional effective weights, and expected-weight coverage."""
    _validate_inventory(values, base_weights)
    if minimum_valid < 1 or minimum_valid > len(values):
        raise ValueError("minimum_valid must be within the Growth inventory")
    valid = [pillar_id for pillar_id, value in values.items() if value is not None]
    available = sum((base_weights[pillar_id] for pillar_id in valid), Decimal(0))
    coverage = available / sum(base_weights.values(), Decimal(0))
    effective = (
        {
            pillar_id: (base_weights[pillar_id] / available if pillar_id in valid else Decimal(0))
            for pillar_id in values
        }
        if available
        else {pillar_id: Decimal(0) for pillar_id in values}
    )
    if len(valid) < minimum_valid:
        return None, effective, coverage
    with localcontext() as context:
        context.prec = 50
        numerator = Decimal(0)
        for pillar_id in valid:
            value = values[pillar_id]
            if value is None:
                raise AssertionError("Valid Growth inventory unexpectedly contains null")
            numerator += base_weights[pillar_id] * value
        score = numerator / available
    if not Decimal(-1) < score < Decimal(1):
        raise ValueError("Growth mean must remain strictly inside (-1, 1)")
    return score, effective, coverage


def expected_weighted_confidence(
    confidences: Mapping[str, Decimal | None], base_weights: Mapping[str, Decimal]
) -> Decimal:
    """Aggregate confidence on expected base weights, treating missing as zero."""
    if set(confidences) != set(base_weights):
        raise ValueError("Growth confidence and weight inventories must match")
    _validate_inventory({key: None for key in confidences}, base_weights)
    if any(
        value is not None and (not value.is_finite() or not Decimal(0) <= value <= Decimal(1))
        for value in confidences.values()
    ):
        raise ValueError("Pillar confidence must be finite and within [0, 1]")
    numerator = Decimal(0)
    for pillar_id, confidence in confidences.items():
        numerator += base_weights[pillar_id] * (
            confidence if confidence is not None else Decimal(0)
        )
    return numerator / sum(base_weights.values(), Decimal(0))


def signed_breadth(
    values: Mapping[str, Decimal | None],
) -> tuple[int, int, int, Decimal | None]:
    """Return exact-zero counts and signed breadth over valid pillars only."""
    valid = [value for value in values.values() if value is not None]
    if not valid:
        return 0, 0, 0, None
    positive = sum(value > 0 for value in valid)
    negative = sum(value < 0 for value in valid)
    neutral = sum(value == 0 for value in valid)
    breadth = Decimal(positive - negative) / Decimal(len(valid))
    return positive, negative, neutral, breadth


def population_std(values: Mapping[str, Decimal | None]) -> Decimal | None:
    """Return population standard deviation over valid Growth pillars."""
    valid = [value for value in values.values() if value is not None]
    if not valid:
        return None
    with localcontext() as context:
        context.prec = 50
        mean = sum(valid, Decimal(0)) / Decimal(len(valid))
        variance = sum(((value - mean) ** 2 for value in valid), Decimal(0)) / Decimal(len(valid))
        return +variance.sqrt()
