"""Pure deterministic Stage 3C contribution and diagnostic calculations."""

from collections import Counter
from decimal import Decimal, localcontext

from app.pillars.schemas import (
    ConcentrationMetadata,
    CoveragePolicy,
    PillarStatus,
    SupportingConfirmation,
    SupportingConfirmationStatus,
    SupportingSignalDefinition,
    SupportingUsage,
    SupportingZone,
)
from app.signals.schemas import NormalizedSignalResult, SignalStatus

ZONE_THRESHOLD = Decimal("0.5")
EXTREME_CONTRIBUTION_THRESHOLD = Decimal(4)


def decimal_tanh(
    value: Decimal,
    *,
    amplitude: Decimal = Decimal(1),
    scale: Decimal = Decimal(2),
) -> Decimal:
    """Return deterministic high-precision tanh saturation without float conversion."""
    if amplitude <= 0 or scale <= 0:
        raise ValueError("Tanh amplitude and scale must be positive")
    if not value.is_finite():
        raise ValueError("Tanh input must be finite")
    if value.is_zero():
        return Decimal(0)
    magnitude = abs(value) / scale
    # Resolving 1 - exp(-2x) requires enough significant digits to retain the
    # exponentially small gap below the amplitude for extreme z-scores.
    required_precision = min(512, max(50, int(magnitude * Decimal("0.9")) + 50))
    with localcontext() as context:
        context.prec = required_precision
        decayed = (-Decimal(2) * magnitude).exp()
        result = amplitude * (Decimal(1) - decayed) / (Decimal(1) + decayed)
        if result == amplitude:
            # Preserve the open interval under the bounded precision budget.
            result = context.next_minus(amplitude)
        return +result if value > 0 else +result.copy_negate()


def supporting_zone(value: Decimal) -> SupportingZone:
    """Classify a directed value with inclusive ±0.5 directional boundaries."""
    if value >= ZONE_THRESHOLD:
        return SupportingZone.POSITIVE
    if value <= -ZONE_THRESHOLD:
        return SupportingZone.NEGATIVE
    return SupportingZone.NEUTRAL


def coverage_status(
    valid_count: int,
    expected_count: int,
    policy: CoveragePolicy,
) -> tuple[Decimal, PillarStatus]:
    """Evaluate exact rational coverage without binary floating-point boundaries."""
    if expected_count <= 0 or valid_count < 0 or valid_count > expected_count:
        raise ValueError("Invalid constituent counts")
    coverage = Decimal(valid_count) / Decimal(expected_count)
    if valid_count == 0:
        return coverage, PillarStatus.NO_VALID_SIGNALS
    if valid_count == expected_count:
        return coverage, PillarStatus.OK
    if valid_count * policy.minimum_denominator >= expected_count * policy.minimum_numerator:
        return coverage, PillarStatus.PARTIAL
    return coverage, PillarStatus.INSUFFICIENT_COVERAGE


def supporting_confirmation(
    primary: NormalizedSignalResult | None,
    definitions: list[SupportingSignalDefinition],
    latest_by_signal: dict[str, NormalizedSignalResult],
    *,
    primary_signal_id: str | None = None,
) -> SupportingConfirmation:
    """Compare directional supporting votes without producing numeric contribution."""
    primary_zone: SupportingZone | None = None
    if (
        primary is not None
        and primary.signal_status is SignalStatus.OK
        and primary.directed_value is not None
    ):
        primary_zone = supporting_zone(primary.directed_value)

    applicable = [
        item for item in definitions if item.intended_usage is not SupportingUsage.NOT_USED_V1
    ]
    zones: list[SupportingZone] = []
    used_ids: list[str] = []
    for definition in applicable:
        row = latest_by_signal.get(definition.signal_id)
        if (
            row is not None
            and row.signal_status is SignalStatus.OK
            and row.directed_value is not None
        ):
            zones.append(supporting_zone(row.directed_value))
            used_ids.append(row.signal_id)

    same = 0
    opposite = 0
    neutral = sum(zone is SupportingZone.NEUTRAL for zone in zones)
    status = SupportingConfirmationStatus.INSUFFICIENT
    if primary_zone is not None and primary_zone is not SupportingZone.NEUTRAL and len(zones) >= 2:
        same = sum(zone is primary_zone for zone in zones)
        opposite_zone = (
            SupportingZone.NEGATIVE
            if primary_zone is SupportingZone.POSITIVE
            else SupportingZone.POSITIVE
        )
        opposite = sum(zone is opposite_zone for zone in zones)
        if same * 2 > len(zones):
            status = SupportingConfirmationStatus.CONFIRMS_PRIMARY
        elif opposite * 2 > len(zones):
            status = SupportingConfirmationStatus.DIVERGES_FROM_PRIMARY
        else:
            status = SupportingConfirmationStatus.MIXED
    return SupportingConfirmation(
        redundancy_group=(
            primary.redundancy_group
            if primary is not None
            else definitions[0].redundancy_group
            if definitions
            else "UNKNOWN"
        ),
        primary_signal_id=(
            primary.signal_id if primary is not None else primary_signal_id or "MISSING"
        ),
        primary_zone=primary_zone,
        valid_supporting_count=len(zones),
        same_direction_count=same,
        opposite_direction_count=opposite,
        neutral_count=neutral,
        status=status,
        supporting_signal_ids=used_ids,
    )


def concentration_metadata(
    primary_rows: list[NormalizedSignalResult | None],
    *,
    expected_primary_count: int,
    expected_axes: list[str],
    expected_groups: list[str],
) -> ConcentrationMetadata:
    """Return inventory-based concentration shares without altering weights."""
    del primary_rows  # Runtime availability never changes frozen expected concentration.
    if expected_primary_count <= 0:
        raise ValueError("A pillar must contain primary signals")
    axis_counts = Counter(expected_axes)
    group_counts = Counter(expected_groups)
    return ConcentrationMetadata(
        primary_count=expected_primary_count,
        axis_count=len(axis_counts),
        largest_axis_share=Decimal(max(axis_counts.values())) / Decimal(expected_primary_count),
        largest_redundancy_group_share=(
            Decimal(max(group_counts.values())) / Decimal(expected_primary_count)
        ),
    )
