"""Atomic gitignored Stage 3F checkpoint persistence."""

from pathlib import Path

from app.validation.schemas import Checkpoint


class CheckpointMismatch(RuntimeError):
    """An existing checkpoint belongs to another authority or schedule."""


def write_checkpoint(path: Path, checkpoint: Checkpoint) -> None:
    """Atomically write a checkpoint without embedding secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)


def load_checkpoint(
    path: Path,
    *,
    validation_spec_hash: str,
    production_authority_hashes: list[str],
    schedule_hash: str,
) -> Checkpoint | None:
    """Load only a checkpoint bound to the exact authority and schedule."""
    if not path.is_file():
        return None
    checkpoint = Checkpoint.model_validate_json(path.read_text(encoding="utf-8"))
    if checkpoint.validation_spec_hash != validation_spec_hash:
        raise CheckpointMismatch("CHECKPOINT_VALIDATION_AUTHORITY_MISMATCH")
    if checkpoint.production_authority_hashes != production_authority_hashes:
        raise CheckpointMismatch("CHECKPOINT_PRODUCTION_AUTHORITY_MISMATCH")
    if checkpoint.schedule_hash != schedule_hash:
        raise CheckpointMismatch("CHECKPOINT_SCHEDULE_MISMATCH")
    return checkpoint
