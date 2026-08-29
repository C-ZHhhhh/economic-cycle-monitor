"""FRED request-attempt guard regression for Stage 3F hard budgets."""

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.data_sources.fred.cache import FredFileCache
from app.data_sources.fred.client import FredClient
from app.data_sources.fred.raw_store import FredRawStore


def _client(
    tmp_path: Path,
    handler: httpx.AsyncBaseTransport,
    guard: object,
    *,
    retries: int = 1,
) -> FredClient:
    return FredClient(
        api_key=SecretStr("offline-only"),
        base_url="https://fred.invalid/fred",
        timeout_seconds=1,
        max_retries=retries,
        retry_base_seconds=0,
        cache=FredFileCache(tmp_path / "cache", ttl_seconds=3600),
        raw_store=FredRawStore(tmp_path / "raw"),
        transport=handler,
        sleep=_no_sleep,
        attempt_guard=guard,  # type: ignore[arg-type]
    )


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_attempt_guard_runs_before_transport_and_receives_no_secret(tmp_path: Path) -> None:
    order: list[str] = []

    def guard(endpoint: str, parameters: dict[str, str], attempt: int) -> None:
        order.append(f"guard:{endpoint}:{attempt}")
        assert "api_key" not in parameters

    def handler(_: httpx.Request) -> httpx.Response:
        order.append("transport")
        return httpx.Response(200, json={"vintage_dates": ["2026-01-01"]})

    async with _client(tmp_path, httpx.MockTransport(handler), guard) as client:
        await client.get_vintage_dates("SERIES")
    assert order == ["guard:series/vintagedates:1", "transport"]


@pytest.mark.asyncio
async def test_attempt_guard_counts_each_retry(tmp_path: Path) -> None:
    attempts: list[int] = []
    calls = 0

    def guard(_endpoint: str, _parameters: dict[str, str], attempt: int) -> None:
        attempts.append(attempt)

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"vintage_dates": ["2026-01-01"]})

    async with _client(tmp_path, httpx.MockTransport(handler), guard) as client:
        await client.get_vintage_dates("SERIES")
    assert attempts == [1, 2]


@pytest.mark.asyncio
async def test_attempt_guard_can_stop_before_transport(tmp_path: Path) -> None:
    def guard(_endpoint: str, _parameters: dict[str, str], _attempt: int) -> None:
        raise RuntimeError("budget")

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not execute")

    async with _client(tmp_path, httpx.MockTransport(handler), guard) as client:
        with pytest.raises(RuntimeError, match="budget"):
            await client.get_vintage_dates("SERIES")


@pytest.mark.asyncio
async def test_file_cache_hit_does_not_consume_attempt_guard(tmp_path: Path) -> None:
    attempts = 0

    def guard(_endpoint: str, _parameters: dict[str, str], _attempt: int) -> None:
        nonlocal attempts
        attempts += 1

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"vintage_dates": ["2026-01-01"]})

    async with _client(tmp_path, httpx.MockTransport(handler), guard) as client:
        await client.get_vintage_dates("SERIES")
        await client.get_vintage_dates("SERIES")
    assert attempts == 1
