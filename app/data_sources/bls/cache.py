"""Secret-free compressed response cache for BLS batch requests."""

import gzip
import hashlib
import json
import time
from pathlib import Path


def bls_cache_key(parameters: dict[str, object]) -> str:
    """Build a stable key while excluding every registration-key spelling."""
    safe = {
        key: value
        for key, value in parameters.items()
        if key.lower() not in {"registrationkey", "registration_key", "api_key"}
    }
    material = json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode()).hexdigest()


class BlsFileCache:
    """Small TTL cache containing provider response bytes only."""

    def __init__(self, root: Path, *, ttl_seconds: int, enabled: bool = True) -> None:
        self.root = root
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled

    def get(self, key: str) -> bytes | None:
        path = self.root / f"{key}.json.gz"
        if not self.enabled or not path.is_file():
            return None
        if self.ttl_seconds == 0 or time.time() - path.stat().st_mtime > self.ttl_seconds:
            return None
        with gzip.open(path, "rb") as handle:
            return handle.read()

    def put(self, key: str, body: bytes) -> None:
        if not self.enabled:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{key}.json.gz"
        temporary = self.root / f".{key}.tmp"
        with gzip.open(temporary, "wb") as handle:
            handle.write(body)
        temporary.replace(target)
