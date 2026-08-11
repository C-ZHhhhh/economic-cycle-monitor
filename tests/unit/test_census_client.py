"""Offline transport and credential-safety tests for Census EITS."""

import gzip
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.data_sources.census.client import CensusClient
from app.data_sources.census.exceptions import (
    CensusBusinessError,
    CensusRequestError,
    CensusResponseError,
)
from app.data_sources.official.storage import OfficialFileCache, OfficialRawStore

TEST_KEY = "not-a-real-census-key"


def _client(
    tmp_path: Path,
    transport: httpx.AsyncBaseTransport,
    *,
    max_retries: int = 0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> CensusClient:
    return CensusClient(
        api_key=SecretStr(TEST_KEY),
        base_url="https://example.test/data",
        timeout_seconds=1,
        max_retries=max_retries,
        retry_base_seconds=0,
        cache=OfficialFileCache(tmp_path / "cache", ttl_seconds=3600),
        raw_store=OfficialRawStore(tmp_path / "raw"),
        transport=transport,
        **({"sleep": sleep} if sleep is not None else {}),
    )


def _data(value: str = "1427") -> list[list[str]]:
    return [
        [
            "cell_value",
            "data_type_code",
            "time_slot_id",
            "time_slot_date",
            "time_slot_name",
            "error_data",
            "category_code",
            "seasonally_adj",
            "geo_level_code",
            "program_code",
            "time",
        ],
        [value, "TOTAL", "06", "2026-06", "June", "no", "ASTARTS", "yes", "US", "RESCONST", "2026"],
    ]


@pytest.mark.asyncio
async def test_metadata_data_cache_and_archive_redact_key(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url.params["key"] == TEST_KEY
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json={"dataset": [{"c_vintage": "2026"}]})
        return httpx.Response(200, json=_data(TEST_KEY))

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        metadata, metadata_payload = await client.get_dataset_metadata("resconst")
        rows, data_payload = await client.get_data("resconst", time="2026-06")
        cached_rows, cached_payload = await client.get_data("resconst", time="2026-06")

    assert metadata
    assert rows[0].cell_value == "***"
    assert cached_rows[0].cell_value == "***"
    assert cached_payload.from_cache
    assert attempts == 2
    assert TEST_KEY not in metadata_payload.model_dump_json()
    assert TEST_KEY not in data_payload.model_dump_json()
    for path in (tmp_path / "raw").rglob("*.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            assert TEST_KEY not in handle.read()


@pytest.mark.asyncio
async def test_full_http_exception_url_never_reaches_safe_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed URL {request.url}", request=request)

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        with pytest.raises(CensusRequestError) as captured:
            await client.get_data("resconst", time="2026-06")

    assert TEST_KEY not in str(captured.value)
    assert "example.test" not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500])
async def test_retry_after_then_success(tmp_path: Path, status: int) -> None:
    attempts = 0
    sleeps: list[float] = []

    async def no_sleep(value: float) -> None:
        sleeps.append(value)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(status, headers={"Retry-After": "0"})
        return httpx.Response(200, json=_data())

    async with _client(
        tmp_path, httpx.MockTransport(handler), max_retries=1, sleep=no_sleep
    ) as client:
        rows, _ = await client.get_data("resconst", time="2026-06")

    assert rows[0].cell_value == "1427"
    assert attempts == 2
    assert sleeps == [0]


@pytest.mark.asyncio
async def test_html_empty_and_business_errors_are_rejected(tmp_path: Path) -> None:
    bodies: list[httpx.Response] = [
        httpx.Response(200, text="<html>failure</html>"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"error": f"bad key {TEST_KEY}"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return bodies.pop(0)

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        with pytest.raises(CensusResponseError, match="HTML"):
            await client.get_data("resconst", time="2026-06")
        with pytest.raises(CensusResponseError, match="empty"):
            await client.get_data("resconst", time="2026-07")
        with pytest.raises(CensusBusinessError) as captured:
            await client.get_data("resconst", time="2026-08")

    assert TEST_KEY not in str(captured.value)
