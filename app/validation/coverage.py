"""Coverage inventory and first-eligible boundary evidence."""

from collections.abc import Iterable, Mapping
from datetime import date

from app.validation.schemas import (
    BoundaryCandidateStatus,
    CoverageBoundary,
    CoverageBoundaryEvidence,
    CoverageSnapshot,
)


def boundary_statuses(snapshot: CoverageSnapshot) -> dict[CoverageBoundary, bool]:
    """Map one layered snapshot to exact full-universe boundary eligibility."""
    raw_full = snapshot.raw_available_count == snapshot.primary_expected_count
    feature_full = raw_full and snapshot.feature_available_count == snapshot.primary_expected_count
    signal_full = (
        feature_full and snapshot.signal_available_count == snapshot.primary_expected_count
    )
    pillar_full = signal_full and snapshot.pillar_available_count == snapshot.pillar_expected_count
    macro_full = pillar_full and snapshot.macro_state_available
    regime_level_full = macro_full and snapshot.regime_level_available
    return {
        CoverageBoundary.RAW_PIT_START: raw_full,
        CoverageBoundary.FEATURE_PIT_START: feature_full,
        CoverageBoundary.SIGNAL_PIT_START: signal_full,
        CoverageBoundary.PILLAR_PIT_START: pillar_full,
        CoverageBoundary.MACRO_STATE_PIT_START: macro_full,
        CoverageBoundary.REGIME_LEVEL_PIT_START: regime_level_full,
        CoverageBoundary.REGIME_TRAJECTORY_PIT_START: (
            regime_level_full and snapshot.regime_trajectory_available
        ),
    }


def discover_boundaries(
    snapshots: Iterable[CoverageSnapshot],
    *,
    authority_hashes: list[str],
) -> tuple[list[CoverageBoundaryEvidence], list[CoverageBoundary]]:
    """Find first eligible tested candidates and preserve prior-candidate evidence."""
    ordered = sorted(snapshots, key=lambda item: item.evaluation_as_of)
    discovered: list[CoverageBoundaryEvidence] = []
    unresolved: list[CoverageBoundary] = []
    for boundary in CoverageBoundary:
        success_index = next(
            (
                index
                for index, snapshot in enumerate(ordered)
                if boundary_statuses(snapshot)[boundary]
            ),
            None,
        )
        if success_index is None:
            unresolved.append(boundary)
            continue
        success = ordered[success_index]
        previous = ordered[success_index - 1] if success_index > 0 else None
        previous_status = None
        if previous is not None:
            previous_status = (
                BoundaryCandidateStatus.SUCCESS
                if boundary_statuses(previous)[boundary]
                else BoundaryCandidateStatus.PARTIAL
            )
        discovered.append(
            CoverageBoundaryEvidence(
                boundary=boundary,
                first_successful_candidate_date=success.evaluation_as_of,
                previous_candidate_date=previous.evaluation_as_of if previous else None,
                previous_candidate_status=previous_status,
                source_evidence=success.source_evidence,
                production_authority_hashes=authority_hashes,
                calculation_hashes=success.calculation_hashes,
            )
        )
    return discovered, unresolved


def full_primary_date(
    snapshots: Iterable[CoverageSnapshot],
    field: str,
) -> date | None:
    """Return the earliest eligible candidate for a named reported boundary."""
    mapping: Mapping[str, CoverageBoundary] = {
        "earliest_common_full_primary_pit_date": CoverageBoundary.SIGNAL_PIT_START,
        "earliest_full_pillar_state_date": CoverageBoundary.PILLAR_PIT_START,
        "earliest_full_regime_level_date": CoverageBoundary.REGIME_LEVEL_PIT_START,
        "earliest_full_regime_trajectory_date": CoverageBoundary.REGIME_TRAJECTORY_PIT_START,
    }
    if field not in mapping:
        raise ValueError(f"Unknown coverage boundary field: {field}")
    boundary = mapping[field]
    for snapshot in sorted(snapshots, key=lambda item: item.evaluation_as_of):
        if boundary_statuses(snapshot)[boundary]:
            return snapshot.evaluation_as_of
    return None
