"""Canonical semantic hashing helpers for Stage 3F artifacts."""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: object) -> str:
    """Serialize semantic data without process- or platform-dependent spacing."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_json_default,
    )


def semantic_hash(value: object) -> str:
    """Return a stable SHA-256 digest over canonical semantic JSON."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def without_runtime_fields(value: dict[str, Any]) -> dict[str, Any]:
    """Drop known non-semantic runtime fields before deterministic hashing."""
    ignored = {
        "started_at",
        "updated_at",
        "computed_at",
        "retrieved_at",
        "runtime_seconds",
        "filesystem_path",
        "request_id",
        "checkpoint_timestamp",
    }
    return {key: item for key, item in value.items() if key not in ignored}
