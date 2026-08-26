"""Fail-closed validation for explicitly approved early-history stitching."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.source_registry.schemas import SourceRegistryPolicy


class StitchingContract(BaseModel):
    """Comparable canonical metadata at a source transition boundary."""

    model_config = ConfigDict(extra="forbid")

    frequency: str
    unit: str
    scale: Decimal
    seasonal_adjustment: str
    base_year: str | int | None


class StitchingAuditSummary(BaseModel):
    """Auditable result of a proposed source transition."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    transition_date: date
    overlap_count: int
    exact_count: int
    rounding_count: int
    max_absolute_difference: Decimal | None
    reasons: list[str] = Field(default_factory=list)


def validate_stitching_boundary(
    policy: SourceRegistryPolicy,
    *,
    primary_contract: StitchingContract,
    fallback_contract: StitchingContract,
    primary_values: dict[date, Decimal],
    fallback_values: dict[date, Decimal],
) -> StitchingAuditSummary:
    """Validate semantics and overlap before any early-history source transition."""
    reasons: list[str] = []
    if policy.history.extension_policy != "FALLBACK_BEFORE_PRIMARY_START":
        reasons.append("POLICY_DOES_NOT_ALLOW_EARLY_HISTORY_FALLBACK")
    for field_name in ("frequency", "unit", "scale", "seasonal_adjustment", "base_year"):
        if getattr(primary_contract, field_name) != getattr(fallback_contract, field_name):
            reasons.append(f"CONTRACT_MISMATCH:{field_name}")
    overlap_dates = sorted(set(primary_values) & set(fallback_values))
    if not overlap_dates:
        reasons.append("NO_OVERLAP_EVIDENCE")
    exact = 0
    rounding = 0
    differences: list[Decimal] = []
    for observation_date in overlap_dates:
        difference = abs(primary_values[observation_date] - fallback_values[observation_date])
        differences.append(difference)
        if difference == 0:
            exact += 1
        elif (
            policy.comparison.source_rounding_allowed
            and difference <= policy.comparison.absolute_tolerance
        ):
            rounding += 1
        else:
            reasons.append(f"UNEXPLAINED_LEVEL_BREAK:{observation_date.isoformat()}")
    return StitchingAuditSummary(
        allowed=not reasons,
        transition_date=policy.history.earliest_primary_date,
        overlap_count=len(overlap_dates),
        exact_count=exact,
        rounding_count=rounding,
        max_absolute_difference=max(differences) if differences else None,
        reasons=reasons,
    )
