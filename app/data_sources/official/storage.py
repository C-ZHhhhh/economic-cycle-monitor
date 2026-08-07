"""Secret-free cache and raw-response storage for official data sources."""

import gzip
import hashlib
import json
import time
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from pathlib import Path


def official_cache_key(parameters: Mapping[str, object], *, secret_names: Collection[str]) -> str:
    """Build a stable cache key after removing provider credential fields."""
    blocked = {name.lower() for name in secret_names}
    safe = {key: value for key, value in parameters.items() if key.lower() not in blocked}
    material = json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode()).hexdigest()


class OfficialFileCache:
    """Small TTL cache containing only sanitized provider response bytes."""

    def __init__(self, root: Path, *, ttl_seconds: int, enabled: bool = True) -> None:
        self.root = root
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled

    def get(self, key: str) -> bytes | None:
        """Return an unexpired cached payload."""
        path = self.root / f"{key}.json.gz"
        if not self.enabled or not path.is_file():
            return None
        if self.ttl_seconds == 0 or time.time() - path.stat().st_mtime > self.ttl_seconds:
            return None
        with gzip.open(path, "rb") as handle:
            return handle.read()

    def put(self, key: str, body: bytes) -> None:
        """Atomically store sanitized response bytes."""
        if not self.enabled:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{key}.json.gz"
        temporary = self.root / f".{key}.tmp"
        with gzip.open(temporary, "wb") as handle:
            handle.write(body)
        temporary.replace(target)


class OfficialRawStore:
    """Archive sanitized JSON under source/date/endpoint/hash paths."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def archive(self, *, body: bytes, retrieved_at: datetime, endpoint: str) -> tuple[str, Path]:
        """Atomically archive one sanitized response and return its hash/path."""
        digest = hashlib.sha256(body).hexdigest()
        timestamp = retrieved_at.astimezone(UTC)
        safe_endpoint = endpoint.strip("/").replace("/", "_") or "response"
        directory = self.root / timestamp.strftime("%Y-%m-%d") / safe_endpoint
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{timestamp.strftime('%H%M%S%f')}-{digest[:12]}.json.gz"
        if not target.exists():
            temporary = directory / f".{target.name}.tmp"
            with gzip.open(temporary, "wb") as handle:
                handle.write(body)
            temporary.replace(target)
        return digest, target
