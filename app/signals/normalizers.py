"""Typed, Decimal-safe Stage 3B production normalizers."""

from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation, localcontext

from app.signals.schemas import SignalQualityFlag, SignalStatus

NORMALIZER_REGISTRY_VERSION = "stage3b-normalizers-v1"
CALCULATION_PRECISION = 34
DISPERSION_ESTIMATOR = "SAMPLE_STANDARD_DEVIATION_DDOF_1"


@dataclass(frozen=True)
class NormalizationOutcome:
    """One raw normalization result before economic direction is applied."""

    normalized_value: Decimal | None
    center: Decimal | None
    scale: Decimal | None
    status: SignalStatus
    quality_flags: tuple[SignalQualityFlag, ...]


def _sample_mean_std(values: list[Decimal]) -> tuple[Decimal, Decimal]:
    """Calculate sample mean/std with a numerically stable Decimal Welford pass."""
    mean = Decimal(0)
    moment = Decimal(0)
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        for count, value in enumerate(values, start=1):
            delta = value - mean
            mean += delta / Decimal(count)
            moment += delta * (value - mean)
        variance = moment / Decimal(len(values) - 1)
        if variance < 0:
            raise InvalidOperation
        return mean, variance.sqrt()


def rolling_zscore(
    current: Decimal,
    history: list[Decimal],
    *,
    full_window: int,
    min_history: int,
    extreme_value_threshold: Decimal,
    low_dispersion_relative_threshold: Decimal,
) -> NormalizationOutcome:
    """Normalize against prior-only trailing history with sample standard deviation."""
    if not current.is_finite() or any(not item.is_finite() for item in history):
        return NormalizationOutcome(None, None, None, SignalStatus.NONFINITE_INPUT, ())
    trailing = history[-full_window:]
    if len(trailing) < min_history:
        return NormalizationOutcome(
            None, None, None, SignalStatus.NORMALIZATION_INSUFFICIENT_HISTORY, ()
        )
    flags: set[SignalQualityFlag] = set()
    if len(trailing) < full_window:
        flags.add(SignalQualityFlag.PARTIAL_WINDOW)
    try:
        center, scale = _sample_mean_std(trailing)
        if scale == 0:
            return NormalizationOutcome(
                None,
                center,
                scale,
                SignalStatus.ZERO_DISPERSION,
                tuple(sorted(flags, key=str)),
            )
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            normalized = (current - center) / scale
    except (DecimalException, InvalidOperation, ZeroDivisionError):
        return NormalizationOutcome(
            None, None, None, SignalStatus.NONFINITE_RESULT, tuple(sorted(flags, key=str))
        )
    if not normalized.is_finite():
        return NormalizationOutcome(
            None,
            center,
            scale,
            SignalStatus.NONFINITE_RESULT,
            tuple(sorted(flags, key=str)),
        )
    reference = max(abs(center), Decimal(1))
    if scale <= reference * low_dispersion_relative_threshold:
        flags.add(SignalQualityFlag.LOW_DISPERSION_RELIABILITY)
    if abs(normalized) >= extreme_value_threshold:
        flags.add(SignalQualityFlag.EXTREME_VALUE)
    return NormalizationOutcome(
        normalized,
        center,
        scale,
        SignalStatus.OK,
        tuple(sorted(flags, key=str)),
    )
