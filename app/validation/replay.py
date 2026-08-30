"""Deterministic Stage 3F replay identities."""

from collections.abc import Sequence

from app.validation.hashing import semantic_hash
from app.validation.schemas import AuthorityBindings, RegimeHistoryRow, UniverseMode


def history_run_hash(
    *,
    validation_spec_hash: str,
    authorities: AuthorityBindings,
    universe_mode: UniverseMode,
    schedule_hash: str,
    rows: Sequence[RegimeHistoryRow],
) -> str:
    """Bind ordered semantic regime results while ignoring all runtime timestamps."""
    ordered = sorted(rows, key=lambda item: item.evaluation_as_of)
    payload = {
        "validation_spec_hash": validation_spec_hash,
        "production_authority_hashes": authorities.ordered_hashes(),
        "universe_mode": universe_mode,
        "schedule_hash": schedule_hash,
        "history": [
            {
                "evaluation_as_of": item.evaluation_as_of,
                "regime_calculation_hash": item.regime_calculation_hash,
                "semantic_validation_status": item.semantic_validation_status,
            }
            for item in ordered
        ],
    }
    return semantic_hash(payload)


def replay_matches(first_hash: str, second_hash: str) -> bool:
    """Return whether two fully bound history replays are identical."""
    return first_hash == second_hash
