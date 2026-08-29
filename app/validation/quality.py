"""Stage 3F hard-gate evidence and future-leakage checks."""

from collections.abc import Iterable
from datetime import date

from app.regimes.schemas import RegimeResult, RegimeStatus
from app.validation.hashing import semantic_hash
from app.validation.schemas import QualityEvidence


def future_dates(as_of: date, dates: Iterable[date | None]) -> list[date]:
    """Return any observation or release dates beyond the evaluated EOD."""
    return sorted(item for item in dates if item is not None and item > as_of)


def leakage_evidence(
    *, evaluation_as_of: date, semantic_dates: Iterable[date | None]
) -> QualityEvidence | None:
    """Create first evidence for a future-leakage hard failure."""
    leaked = future_dates(evaluation_as_of, semantic_dates)
    if not leaked:
        return None
    detail = f"future dates after {evaluation_as_of.isoformat()}: {leaked[0].isoformat()}"
    return QualityEvidence(
        gate="FUTURE_LEAKAGE",
        evaluation_as_of=evaluation_as_of,
        detail=detail,
        evidence_hash=semantic_hash(
            {"gate": "FUTURE_LEAKAGE", "as_of": evaluation_as_of, "first": leaked[0]}
        ),
    )


def regime_quality_evidence(result: RegimeResult) -> list[QualityEvidence]:
    """Validate PIT safety and current/lag date identities for one classified result."""
    evidence: list[QualityEvidence] = []
    classified = result.base_regime_id is not None
    if classified and (
        not result.is_point_in_time_safe
        or result.regime_status in {RegimeStatus.PIT_UNSAFE, RegimeStatus.AS_OF_MISMATCH}
    ):
        detail = "classified regime is not date-EOD PIT safe"
        evidence.append(
            QualityEvidence(
                gate="CLASSIFIED_RESULT_PIT_UNSAFE",
                evaluation_as_of=result.evaluation_as_of,
                detail=detail,
                evidence_hash=semantic_hash(
                    {
                        "gate": "CLASSIFIED_RESULT_PIT_UNSAFE",
                        "as_of": result.evaluation_as_of,
                        "hash": result.calculation_hash,
                    }
                ),
            )
        )
    for axis_name, trajectory in (
        ("growth", result.growth_trajectory),
        ("inflation", result.inflation_trajectory),
    ):
        if trajectory.current_as_of != result.evaluation_as_of:
            detail = f"{axis_name} current endpoint differs from regime EOD"
            evidence.append(
                QualityEvidence(
                    gate="CURRENT_LAG_SAME_AS_OF_VIOLATION",
                    evaluation_as_of=result.evaluation_as_of,
                    detail=detail,
                    evidence_hash=semantic_hash(
                        {
                            "gate": "CURRENT_LAG_SAME_AS_OF_VIOLATION",
                            "axis": axis_name,
                            "current": trajectory.current_as_of,
                            "expected": result.evaluation_as_of,
                        }
                    ),
                )
            )
    return evidence


def contains_credential(text: str, known_secret: str | None = None) -> bool:
    """Detect a known secret or credential parameter without returning its value."""
    lowered = text.lower()
    if known_secret and known_secret in text:
        return True
    return any(token in lowered for token in ("api_key=", '"api_key":', "authorization: bearer"))
