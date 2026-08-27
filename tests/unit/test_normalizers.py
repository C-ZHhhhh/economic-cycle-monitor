"""Decimal rolling normalization contract tests."""

from decimal import Decimal

import pytest

from app.signals.normalizers import NormalizationOutcome, _sample_mean_std, rolling_zscore
from app.signals.schemas import SignalQualityFlag, SignalStatus


def _normalize(
    current: str,
    history: list[Decimal],
    *,
    full_window: int = 120,
    min_history: int = 60,
) -> NormalizationOutcome:
    return rolling_zscore(
        Decimal(current),
        history,
        full_window=full_window,
        min_history=min_history,
        extreme_value_threshold=Decimal(4),
        low_dispersion_relative_threshold=Decimal("1e-12"),
    )


def test_sample_standard_deviation_uses_ddof_one() -> None:
    center, scale = _sample_mean_std([Decimal(1), Decimal(2), Decimal(3)])
    assert center == Decimal(2)
    assert scale == Decimal(1)


def test_current_observation_is_not_part_of_distribution() -> None:
    outcome = _normalize("4", [Decimal(1), Decimal(2), Decimal(3)], full_window=3, min_history=2)
    assert outcome.center == Decimal(2)
    assert outcome.scale == Decimal(1)
    assert outcome.normalized_value == Decimal(2)


def test_min_history_gate_returns_typed_status() -> None:
    outcome = _normalize("60", [Decimal(index) for index in range(59)])
    assert outcome.status is SignalStatus.NORMALIZATION_INSUFFICIENT_HISTORY
    assert outcome.normalized_value is None


def test_growing_window_adds_partial_flag() -> None:
    outcome = _normalize("60", [Decimal(index) for index in range(60)])
    assert outcome.status is SignalStatus.OK
    assert SignalQualityFlag.PARTIAL_WINDOW in outcome.quality_flags


def test_full_window_has_no_partial_flag() -> None:
    outcome = _normalize("120", [Decimal(index) for index in range(120)])
    assert outcome.status is SignalStatus.OK
    assert SignalQualityFlag.PARTIAL_WINDOW not in outcome.quality_flags


def test_only_latest_full_window_is_used() -> None:
    history = [Decimal(-1000)] + [Decimal(index) for index in range(1, 121)]
    outcome = _normalize("121", history)
    expected_center, expected_scale = _sample_mean_std(history[-120:])
    assert outcome.center == expected_center
    assert outcome.scale == expected_scale


def test_zero_dispersion_fails_without_epsilon_hack() -> None:
    outcome = _normalize("2", [Decimal(1)] * 60)
    assert outcome.status is SignalStatus.ZERO_DISPERSION
    assert outcome.normalized_value is None
    assert outcome.scale == 0


def test_low_nonzero_dispersion_is_preserved_and_flagged() -> None:
    history = [
        Decimal("1.0000000000001") + Decimal(index) * Decimal("1e-16") for index in range(60)
    ]
    outcome = _normalize("1.000000000001", history)
    assert outcome.status is SignalStatus.OK
    assert outcome.normalized_value is not None
    assert SignalQualityFlag.LOW_DISPERSION_RELIABILITY in outcome.quality_flags


def test_extreme_value_is_flagged_but_never_clipped() -> None:
    outcome = _normalize("1000", [Decimal(index) for index in range(60)])
    assert outcome.status is SignalStatus.OK
    assert outcome.normalized_value is not None and outcome.normalized_value > 4
    assert outcome.normalized_value != Decimal(4)
    assert SignalQualityFlag.EXTREME_VALUE in outcome.quality_flags


@pytest.mark.parametrize("nonfinite", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_current_fails_closed(nonfinite: str) -> None:
    outcome = _normalize(nonfinite, [Decimal(index) for index in range(60)])
    assert outcome.status is SignalStatus.NONFINITE_INPUT


def test_nonfinite_history_fails_closed() -> None:
    outcome = _normalize("1", [Decimal(index) for index in range(59)] + [Decimal("NaN")])
    assert outcome.status is SignalStatus.NONFINITE_INPUT


def test_decimal_precision_is_not_presentation_rounded() -> None:
    history = [Decimal("100.000000000000000000000000000001") + index for index in range(60)]
    outcome = _normalize("161.12345678901234567890123456789", history)
    assert outcome.status is SignalStatus.OK
    assert outcome.normalized_value is not None
    assert len(outcome.normalized_value.as_tuple().digits) > 10
