"""Pure offline Stage 3F counterfactual and hysteresis functions."""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from app.regimes.calculations import (
    classify_growth,
    classify_growth_trajectory,
    classify_inflation,
    classify_inflation_trajectory,
)
from app.validation.schemas import ExactRatio, RegimeHistoryRow


def counterfactual_level(
    row: RegimeHistoryRow,
    *,
    neutral_band: Decimal,
    confidence_gate: ExactRatio,
) -> dict[str, str | None]:
    """Reclassify cached semantic values without touching production history."""
    if neutral_band < 0:
        raise ValueError("neutral_band must be non-negative")
    if (
        row.growth_strength is None
        or row.inflation_pressure is None
        or not confidence_gate.accepts(row.minimum_axis_data_confidence)
    ):
        return {"growth_condition": None, "inflation_condition": None, "base_regime_id": None}
    growth = classify_growth(row.growth_strength, lower=-neutral_band, upper=neutral_band).value
    inflation = classify_inflation(
        row.inflation_pressure, lower=-neutral_band, upper=neutral_band
    ).value
    inflation_id = inflation.removesuffix("_PRESSURE")
    return {
        "growth_condition": growth,
        "inflation_condition": inflation,
        "base_regime_id": (f"US_MACRO_REGIME__GROWTH_{growth}__INFLATION_PRESSURE_{inflation_id}"),
    }


def counterfactual_trajectory(
    rows: Sequence[RegimeHistoryRow],
    *,
    horizon_months: int,
    stable_band: Decimal,
) -> list[dict[str, object]]:
    """Use cached Stage 3D axis values at exact calendar-month lag endpoints."""
    if horizon_months < 1 or stable_band < 0:
        raise ValueError("invalid trajectory counterfactual parameters")
    by_date = {item.evaluation_as_of: item for item in rows}
    output: list[dict[str, object]] = []
    for current in sorted(rows, key=lambda item: item.evaluation_as_of):
        lag_date = current.evaluation_as_of - relativedelta(months=horizon_months)
        lag = by_date.get(lag_date)
        growth_delta = (
            current.growth_strength - lag.growth_strength
            if lag is not None
            and current.growth_strength is not None
            and lag.growth_strength is not None
            else None
        )
        inflation_delta = (
            current.inflation_pressure - lag.inflation_pressure
            if lag is not None
            and current.inflation_pressure is not None
            and lag.inflation_pressure is not None
            else None
        )
        output.append(
            {
                "evaluation_as_of": current.evaluation_as_of,
                "lag_as_of": lag_date,
                "growth_delta": growth_delta,
                "growth_trajectory": (
                    classify_growth_trajectory(
                        growth_delta, lower=-stable_band, upper=stable_band
                    ).value
                    if growth_delta is not None
                    else None
                ),
                "inflation_delta": inflation_delta,
                "inflation_trajectory": (
                    classify_inflation_trajectory(
                        inflation_delta, lower=-stable_band, upper=stable_band
                    ).value
                    if inflation_delta is not None
                    else None
                ),
            }
        )
    return output


def apply_hysteresis(regimes: Sequence[str | None], policy: str) -> list[str | None]:
    """Simulate a small approved hysteresis family without choosing a winner."""
    if policy not in {"NONE", "ENTER_EXIT", "2_CONSECUTIVE", "3_CONSECUTIVE"}:
        raise ValueError("unsupported hysteresis research policy")
    if policy == "NONE":
        return list(regimes)
    required = {"ENTER_EXIT": 2, "2_CONSECUTIVE": 2, "3_CONSECUTIVE": 3}[policy]
    if not regimes:
        return []
    accepted = regimes[0]
    candidate: str | None = None
    candidate_count = 0
    output = [accepted]
    for proposed in regimes[1:]:
        if proposed == accepted:
            candidate = None
            candidate_count = 0
        elif proposed == candidate:
            candidate_count += 1
        else:
            candidate = proposed
            candidate_count = 1
        if candidate_count >= required:
            accepted = candidate
            candidate = None
            candidate_count = 0
        output.append(accepted)
    return output


def hysteresis_metrics(
    dates: Sequence[date], regimes: Sequence[str | None], policy: str
) -> dict[str, object]:
    """Report transitions, short whipsaws, durations, and occupancy changes."""
    if len(dates) != len(regimes):
        raise ValueError("dates and regimes must have equal length")
    simulated = apply_hysteresis(regimes, policy)
    transitions = sum(a != b for a, b in zip(simulated, simulated[1:], strict=False))
    whipsaws = sum(
        simulated[index - 1] == simulated[index + 1] and simulated[index] != simulated[index - 1]
        for index in range(1, len(simulated) - 1)
    )
    occupancy: dict[str, int] = {}
    for item in simulated:
        key = item or "UNCLASSIFIED"
        occupancy[key] = occupancy.get(key, 0) + 1
    return {
        "policy": policy,
        "transition_count": transitions,
        "whipsaw_count": whipsaws,
        "event_count_occupancy": occupancy,
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
    }


def limited_revision_pairs(
    evidence: Sequence[dict[str, object]], *, permitted_indicators: set[str]
) -> list[dict[str, object]]:
    """Keep representative pairwise evidence; never build an alternate history."""
    return [
        item
        for item in evidence
        if item.get("indicator_id") in permitted_indicators
        and "first_known_hash" in item
        and "later_vintage_hash" in item
    ]
