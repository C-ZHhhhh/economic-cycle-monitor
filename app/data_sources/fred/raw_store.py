"""Immutable compressed archive for provider responses."""

import gzip
import hashlib
from datetime import UTC, datetime
from pathlib import Path


class FredRawStore:
    """Archive raw JSON using hash-addressed, date-partitioned paths."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def archive(
        self, *, endpoint: str, series_id: str | None, body: bytes, retrieved_at: datetime
    ) -> tuple[str, Path]:
        digest = hashlib.sha256(body).hexdigest()
        timestamp = retrieved_at.astimezone(UTC)
        safe_endpoint = endpoint.strip("/").replace("/", "_")
        safe_series = series_id or "shared"
        directory = self.root / timestamp.strftime("%Y-%m-%d") / safe_endpoint / safe_series
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{timestamp.strftime('%H%M%S%f')}-{digest[:12]}.json.gz"
        if not target.exists():
            temporary = directory / f".{target.name}.tmp"
            with gzip.open(temporary, "wb") as handle:
                handle.write(body)
            temporary.replace(target)
        return digest, target
