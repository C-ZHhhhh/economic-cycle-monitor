"""Compatibility wrapper for shared official-source raw storage."""

from datetime import datetime
from pathlib import Path

from app.data_sources.official.storage import OfficialRawStore


class BlsRawStore:
    """Archive raw JSON under source/date/hash paths without credentials."""

    def __init__(self, root: Path) -> None:
        self._store = OfficialRawStore(root)

    def archive(self, *, body: bytes, retrieved_at: datetime) -> tuple[str, Path]:
        """Archive a BLS time-series response."""
        return self._store.archive(
            body=body,
            retrieved_at=retrieved_at,
            endpoint="timeseries_data",
        )
