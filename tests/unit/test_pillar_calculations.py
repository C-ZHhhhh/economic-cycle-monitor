"""Deterministic Stage 3C tanh, coverage, and confirmation tests."""

from datetime import date
from decimal import Decimal

import pytest

from app.pillars.calculations import (
    coverage_status,
    decimal_tanh,
    supporting_confirmation,
    supporting_zone,
)
from app.pillars.schemas import (
    CoveragePolicy,
    PillarStatus,
    SupportingConfirmationStatus,
    SupportingSignalDefinition,
    SupportingUsage,
    SupportingZone,
)
from app.signals.config import load_signal_catalog
from app.signals.schemas import NormalizedSignalResult, SignalStatus

SIGNALS = load_signal_catalog()


def _row(signal_id: str, directed: Decimal | None) -> NormalizedSignalResult:
    definition = SIGNALS.by_signal()[signal_id]
    return NormalizedSignalResult(
        signal_id=signal_id,
        feature_id=definition.feature_id,
        feature_spec_version="stage3a-v1",
        normalization_spec_version="stage3b-v1",
        normalization_spec_hash=SIGNALS.specification_hash,
        indicator_id=definition.indicator_id,
        economic_axis=definition.economic_axis,
        feature_role=definition.feature_role,
        redundancy_group=definition.redundancy_group,
        feature_observation_date=date(2025, 1, 1),
        as_of=None,
        raw_feature_value=directed,
        raw_feature_unit="fixture",
        normalized_value=directed,
        directed_value=directed,
        normalized_unit="z_score",
        normalization_method=definition.normalization_method,
        window=definition.full_window,
        min_history=definition.min_history,
        history_start=date(2020, 1, 1),
        history_end=date(2024, 12, 1),
        history_count=definition.min_history,
        window_coverage=Decimal(1),
        center=Decimal(0),
        scale=Decimal(1),
        dispersion_estimator="fixture",
        distribution_includes_current=False,
        direction=definition.direction,
        direction_multiplier=definition.direction_multiplier,
        source_policy_version="stage2b5-v3",
        registry_hash=("7a13095907733a1d6092ef5231c67de7528886c7eda9eeb73ed2166764834758"),
        feature_calculation_hash=f"feature:{signal_id}",
        input_feature_calculation_hashes=[f"feature:{signal_id}"],
        history_feature_calculation_hash=f"history:{signal_id}",
        is_point_in_time_safe=False,
        signal_status=SignalStatus.OK,
        quality_flags=[],
        calculation_hash=f"signal:{signal_id}:{directed}",
    )


@pytest.mark.parametrize(
    ("value", "expected_sign"),
    [
        (Decimal("-100"), -1),
        (Decimal("-10"), -1),
        (Decimal("-5"), -1),
        (Decimal("-1"), -1),
        (Decimal("0"), 0),
        (Decimal("1"), 1),
        (Decimal("5"), 1),
        (Decimal("10"), 1),
        (Decimal("100"), 1),
        (Decimal("151.4766029807837722861885271"), 1),
        (Decimal("-151.4766029807837722861885271"), -1),
        (Decimal("1e6"), 1),
        (Decimal("-1e6"), -1),
    ],
)
def test_decimal_tanh_is_strictly_bounded(value: Decimal, expected_sign: int) -> None:
    result = decimal_tanh(value)
    assert Decimal(-1) < result < Decimal(1)
    assert (result > 0) - (result < 0) == expected_sign


@pytest.mark.parametrize("value", [Decimal("0.1"), Decimal("1"), Decimal("4"), Decimal("10")])
def test_decimal_tanh_is_exactly_symmetric_and_repeatable(value: Decimal) -> None:
    first = decimal_tanh(value)
    assert decimal_tanh(-value) == first.copy_negate()
    assert decimal_tanh(value) == first
    assert str(decimal_tanh(value)) == str(first)


def test_decimal_tanh_is_monotonic_and_not_prematurely_rounded() -> None:
    values = [decimal_tanh(Decimal(item)) for item in ("-10", "-4", "0", "4", "10")]
    assert values == sorted(values)
    exponent = decimal_tanh(Decimal(1)).as_tuple().exponent
    assert isinstance(exponent, int)
    assert exponent < -20


def test_decimal_tanh_rejects_invalid_parameters_and_input() -> None:
    with pytest.raises(ValueError, match="positive"):
        decimal_tanh(Decimal(1), scale=Decimal(0))
    with pytest.raises(ValueError, match="finite"):
        decimal_tanh(Decimal("NaN"))


@pytest.mark.parametrize(
    ("valid", "expected", "coverage", "status"),
    [
        (3, 3, Decimal(1), PillarStatus.OK),
        (2, 3, Decimal(2) / Decimal(3), PillarStatus.PARTIAL),
        (7, 11, Decimal(7) / Decimal(11), PillarStatus.INSUFFICIENT_COVERAGE),
        (8, 11, Decimal(8) / Decimal(11), PillarStatus.PARTIAL),
        (0, 4, Decimal(0), PillarStatus.NO_VALID_SIGNALS),
        (1, 1, Decimal(1), PillarStatus.OK),
    ],
)
def test_coverage_uses_exact_rational_boundaries(
    valid: int, expected: int, coverage: Decimal, status: PillarStatus
) -> None:
    actual, actual_status = coverage_status(
        valid, expected, CoveragePolicy(minimum_numerator=2, minimum_denominator=3)
    )
    assert actual == coverage
    assert actual_status is status


@pytest.mark.parametrize(
    ("value", "zone"),
    [
        (Decimal("0.5"), SupportingZone.POSITIVE),
        (Decimal("0.50001"), SupportingZone.POSITIVE),
        (Decimal("0.49999"), SupportingZone.NEUTRAL),
        (Decimal("0"), SupportingZone.NEUTRAL),
        (Decimal("-0.49999"), SupportingZone.NEUTRAL),
        (Decimal("-0.5"), SupportingZone.NEGATIVE),
        (Decimal("-0.50001"), SupportingZone.NEGATIVE),
    ],
)
def test_supporting_zone_boundaries(value: Decimal, zone: SupportingZone) -> None:
    assert supporting_zone(value) is zone


def _confirmation(
    values: list[Decimal], primary_value: Decimal = Decimal(1)
) -> SupportingConfirmationStatus:
    primary_id = "US_REAL_GDP__GROWTH_QOQ_ANNUALIZED__REAL_ACTIVITY_STRENGTH__SIGNAL"
    supporting_ids = [
        "US_REAL_GDP__GROWTH_YOY__REAL_ACTIVITY_STRENGTH__SIGNAL",
        "US_REAL_GDP__GROWTH_ACCEL_QOQ_1Q__REAL_ACTIVITY_STRENGTH__SIGNAL",
    ]
    definitions = [
        SupportingSignalDefinition(
            signal_id=signal_id,
            related_pillar_id="US_MACRO__REAL_ACTIVITY_AND_DEMAND",
            redundancy_group="GDP_HORIZONS",
            intended_usage=SupportingUsage.CONFIRMATION,
        )
        for signal_id in supporting_ids
    ]
    rows = {
        signal_id: _row(signal_id, value)
        for signal_id, value in zip(supporting_ids, values, strict=False)
    }
    return supporting_confirmation(_row(primary_id, primary_value), definitions, rows).status


@pytest.mark.parametrize(
    ("supporting", "primary", "status"),
    [
        ([Decimal(1), Decimal(2)], Decimal(1), SupportingConfirmationStatus.CONFIRMS_PRIMARY),
        (
            [Decimal(-1), Decimal(-2)],
            Decimal(1),
            SupportingConfirmationStatus.DIVERGES_FROM_PRIMARY,
        ),
        ([Decimal(1), Decimal(-1)], Decimal(1), SupportingConfirmationStatus.MIXED),
        ([Decimal(1)], Decimal(1), SupportingConfirmationStatus.INSUFFICIENT),
        ([Decimal(1), Decimal(2)], Decimal(0), SupportingConfirmationStatus.INSUFFICIENT),
        ([Decimal(0), Decimal(1)], Decimal(1), SupportingConfirmationStatus.MIXED),
        ([Decimal(-1), Decimal(-2)], Decimal(-1), SupportingConfirmationStatus.CONFIRMS_PRIMARY),
    ],
)
def test_supporting_majority_rules(
    supporting: list[Decimal],
    primary: Decimal,
    status: SupportingConfirmationStatus,
) -> None:
    assert _confirmation(supporting, primary) is status
