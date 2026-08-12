"""Common raw/cache formats used by public official file providers."""

import gzip
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.data_sources.official.storage import OfficialFileCache, OfficialRawStore


@pytest.mark.parametrize("suffix", ["txt.gz", "xml.gz", "zip", "bin.gz"])
def test_common_cache_and_raw_store_round_trip(tmp_path: Path, suffix: str) -> None:
    body = b"PK\x03\x04fixture" if suffix == "zip" else b"official payload"
    cache = OfficialFileCache(tmp_path / "cache", ttl_seconds=3600, enabled=True, suffix=suffix)
    raw = OfficialRawStore(tmp_path / "raw", suffix=suffix)

    cache.put("a" * 64, body)
    digest, path = raw.archive(
        body=body, retrieved_at=datetime.now(UTC), endpoint="official/release"
    )

    assert cache.get("a" * 64) == body
    if suffix == "zip":
        archived = path.read_bytes()
    else:
        with gzip.open(path, "rb") as handle:
            archived = handle.read()
    assert archived == body
    assert len(digest) == 64
    assert path.name.endswith(suffix)


def test_unknown_storage_suffix_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported official-source storage suffix"):
        OfficialFileCache(tmp_path, ttl_seconds=1, enabled=True, suffix="html")
