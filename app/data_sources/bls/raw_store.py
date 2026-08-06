"""Immutable compressed archive for BLS batch responses."""

import gzip
import hashlib
from datetime import UTC, datetime
from pathlib import Path


class BlsRawStore:
    """Archive raw JSON under source/date/hash paths without credentials."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def archive(self, *, body: bytes, retrieved_at: datetime) -> tuple[str, Path]:
        digest = hashlib.sha256(body).hexdigest()
        timestamp = retrieved_at.astimezone(UTC)
        directory = self.root / timestamp.strftime("%Y-%m-%d") / "timeseries_data"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{timestamp.strftime('%H%M%S%f')}-{digest[:12]}.json.gz"
        if not target.exists():
            temporary = directory / f".{target.name}.tmp"
            with gzip.open(temporary, "wb") as handle:
                handle.write(body)
            temporary.replace(target)
        return digest, target
