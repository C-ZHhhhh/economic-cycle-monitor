"""Stage 3D conditional mean, confidence, breadth, and dispersion tests."""

from decimal import Decimal

import pytest

from app.composites.calculations import (
    conditional_weighted_mean,
    expected_weighted_confidence,
    population_std,
    signed_breadth,
)

IDS = ("activity", "labor", "manufacturing", "housing")
WEIGHTS = {pillar_id: Decimal("0.25") for pillar_id in IDS}


def test_full_growth_uses_equal_base_and_effective_weights() -> None:
    values = {
        "activity": Decimal("0.2"),
        "labor": Decimal("0.4"),
        "manufacturing": Decimal("-0.2"),
        "housing": Decimal("0"),
    }
    score, effective, coverage = conditional_weighted_mean(values, WEIGHTS, minimum_valid=3)
    assert score == Decimal("0.1")
    assert coverage == Decimal(1)
    assert set(effective.values()) == {Decimal("0.25")}


def test_three_of_four_conditionally_normalizes_without_missing_as_zero() -> None:
    values = {
        "activity": Decimal("-0.45"),
        "labor": None,
        "manufacturing": Decimal("-0.87"),
        "housing": Decimal("-0.91"),
    }
    score, effective, coverage = conditional_weighted_mean(values, WEIGHTS, minimum_valid=3)
    expected = sum((value for value in values.values() if value is not None), Decimal(0)) / Decimal(
        3
    )
    assert score is not None
    assert abs(score - expected) < Decimal("1e-27")
    assert score != sum(value or Decimal(0) for value in values.values()) / 4
    assert coverage == Decimal("0.75")
    assert effective["labor"] == 0
    assert {effective[item] for item in IDS if item != "labor"} == {Decimal(1) / 3}


@pytest.mark.parametrize("valid_count", [0, 1, 2])
def test_below_quorum_returns_null_value(valid_count: int) -> None:
    values = {
        pillar_id: Decimal("0.5") if index < valid_count else None
        for index, pillar_id in enumerate(IDS)
    }
    score, effective, coverage = conditional_weighted_mean(values, WEIGHTS, minimum_valid=3)
    assert score is None
    assert coverage == Decimal(valid_count) / 4
    assert (
        all(
            effective[pillar_id] == (Decimal(1) / valid_count if index < valid_count else 0)
            for index, pillar_id in enumerate(IDS)
        )
        if valid_count
        else all(value == 0 for value in effective.values())
    )


def test_growth_is_not_second_saturated_or_rounded() -> None:
    values = {pillar_id: Decimal("0.9") for pillar_id in IDS}
    score, _, _ = conditional_weighted_mean(values, WEIGHTS, minimum_valid=3)
    assert score == Decimal("0.9")


def test_expected_weight_confidence_keeps_missing_at_zero() -> None:
    confidences = {
        "activity": Decimal(1),
        "labor": None,
        "manufacturing": Decimal("0.8"),
        "housing": Decimal(1),
    }
    assert expected_weighted_confidence(confidences, WEIGHTS) == Decimal("0.70")


def test_confidence_does_not_weight_growth_score() -> None:
    values = {pillar_id: Decimal(value) for pillar_id, value in zip(IDS, "1234", strict=True)}
    bounded = {pillar_id: value / 10 for pillar_id, value in values.items()}
    score, _, _ = conditional_weighted_mean(bounded, WEIGHTS, minimum_valid=3)
    confidence = expected_weighted_confidence(
        {
            "activity": Decimal("0.1"),
            "labor": Decimal(1),
            "manufacturing": Decimal(1),
            "housing": Decimal(1),
        },
        WEIGHTS,
    )
    assert score == Decimal("0.25")
    assert confidence == Decimal("0.775")


@pytest.mark.parametrize(
    ("values", "counts", "breadth"),
    [
        (["0.1", "0.2", "0.3", "0.4"], (4, 0, 0), Decimal(1)),
        (["-0.1", "-0.2", "-0.3", "-0.4"], (0, 4, 0), Decimal(-1)),
        (["0.1", "-0.2", "0", "0"], (1, 1, 2), Decimal(0)),
    ],
)
def test_signed_breadth_uses_exact_zero_neutral(
    values: list[str], counts: tuple[int, int, int], breadth: Decimal
) -> None:
    mapping = {pillar_id: Decimal(value) for pillar_id, value in zip(IDS, values, strict=True)}
    positive, negative, neutral, actual = signed_breadth(mapping)
    assert (positive, negative, neutral) == counts
    assert actual == breadth


def test_breadth_ignores_missing_pillars() -> None:
    values = dict.fromkeys(IDS)
    values.update(activity=Decimal("0.1"), labor=Decimal("-0.1"))
    assert signed_breadth(values) == (1, 1, 0, Decimal(0))


def test_population_dispersion_uses_ddof_zero() -> None:
    values = {
        "activity": Decimal("-0.5"),
        "labor": Decimal("0.5"),
        "manufacturing": None,
        "housing": None,
    }
    assert population_std(values) == Decimal("0.5")


def test_empty_diagnostics_are_null() -> None:
    values = dict.fromkeys(IDS)
    assert signed_breadth(values) == (0, 0, 0, None)
    assert population_std(values) is None


@pytest.mark.parametrize(
    ("values", "weights"),
    [
        ({"a": Decimal("0.1")}, {"b": Decimal(1)}),
        ({"a": Decimal("0.1")}, {"a": Decimal(0)}),
        ({"a": Decimal("1")}, {"a": Decimal(1)}),
    ],
)
def test_invalid_growth_inputs_fail_closed(
    values: dict[str, Decimal | None], weights: dict[str, Decimal]
) -> None:
    with pytest.raises(ValueError):
        conditional_weighted_mean(values, weights, minimum_valid=1)
