"""Offline tests for FRED transport, retry, cache, and parsing behavior."""

import gzip
import json
import os
import time
from datetime import date
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.data_sources.fred.cache import FredFileCache, request_cache_key
from app.data_sources.fred.client import FredClient
from app.data_sources.fred.exceptions import FredConfigurationError, FredRequestError
from app.data_sources.fred.raw_store import FredRawStore

TEST_KEY = "offline-test-credential"


def _client(
    tmp_path: Path,
    handler: httpx.AsyncBaseTransport,
    *,
    retries: int = 2,
    sleep: object | None = None,
) -> FredClient:
    options: dict[str, object] = {}
    if sleep is not None:
        options["sleep"] = sleep
    return FredClient(
        api_key=SecretStr(TEST_KEY),
        base_url="https://fred.invalid/fred",
        timeout_seconds=1,
        max_retries=retries,
        retry_base_seconds=0,
        cache=FredFileCache(tmp_path / "cache", ttl_seconds=3600),
        raw_store=FredRawStore(tmp_path / "raw"),
        transport=handler,
        **options,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_metadata_request_is_archived_and_audit_parameters_are_redacted(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == TEST_KEY
        return httpx.Response(
            200,
            json={
                "seriess": [
                    {
                        "id": "GDPC1",
                        "realtime_start": "2026-01-01",
                        "realtime_end": "2026-01-01",
                        "title": "Real Gross Domestic Product",
                        "observation_start": "1947-01-01",
                        "observation_end": "2025-10-01",
                        "frequency": "Quarterly",
                        "frequency_short": "Q",
                        "units": "Billions of Chained 2017 Dollars",
                        "units_short": "Bil. of Chn. 2017 $",
                        "seasonal_adjustment": "Seasonally Adjusted Annual Rate",
                        "seasonal_adjustment_short": "SAAR",
                        "last_updated": "2026-01-22 07:48:01-06",
                        "popularity": 92,
                        "notes": "Test fixture only.",
                    }
                ]
            },
        )

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        metadata, payload = await client.get_series_metadata("GDPC1")

    assert metadata.id == "GDPC1"
    assert payload.sanitized_parameters == {"series_id": "GDPC1", "file_type": "json"}
    assert TEST_KEY not in json.dumps(payload.model_dump(mode="json"))
    assert payload.response_path is not None
    with gzip.open(payload.response_path, "rb") as archived:
        assert b'"GDPC1"' in archived.read()


@pytest.mark.asyncio
async def test_observations_parse_missing_values_without_network_fallback(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "observations": [
                    {
                        "realtime_start": "2026-01-01",
                        "realtime_end": "2026-01-01",
                        "date": "2025-01-01",
                        "value": "123.45",
                    },
                    {
                        "realtime_start": "2026-01-01",
                        "realtime_end": "2026-01-01",
                        "date": "2025-04-01",
                        "value": ".",
                    },
                ]
            },
        )

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        first, first_payload = await client.get_observations(
            "GDPC1", observation_start=date(2025, 1, 1)
        )
        second, second_payload = await client.get_observations(
            "GDPC1", observation_start=date(2025, 1, 1)
        )

    assert [item.value for item in first] == [first[0].value, None]
    assert first[0].value is not None and str(first[0].value) == "123.45"
    assert second == first
    assert calls == 1
    assert not first_payload.from_cache
    assert second_payload.from_cache


@pytest.mark.asyncio
async def test_retryable_status_honors_retry_after_without_real_sleep(tmp_path: Path) -> None:
    calls = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"vintage_dates": ["2025-01-15"]})

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async with _client(tmp_path, httpx.MockTransport(handler), sleep=record_sleep) as client:
        dates, payload = await client.get_vintage_dates("GDPC1")

    assert dates == [date(2025, 1, 15)]
    assert calls == 2
    assert delays == [2]
    assert payload.attempt_count == 2


@pytest.mark.asyncio
async def test_uncached_request_without_key_fails_before_transport(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called")

    client = FredClient(
        api_key=None,
        base_url="https://fred.invalid/fred",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        cache=FredFileCache(tmp_path / "cache", ttl_seconds=3600),
        raw_store=FredRawStore(tmp_path / "raw"),
        transport=httpx.MockTransport(handler),
    )
    async with client:
        with pytest.raises(FredConfigurationError, match="FRED_API_KEY"):
            await client.get_vintage_dates("GDPC1")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [500, "timeout"])
async def test_server_error_and_timeout_retry_then_succeed(
    tmp_path: Path, failure: int | str
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("offline timeout", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json={"vintage_dates": []})

    async def no_sleep(_: float) -> None:
        return None

    async with _client(tmp_path, httpx.MockTransport(handler), sleep=no_sleep) as client:
        values, payload = await client.get_vintage_dates("GDPC1")

    assert values == []
    assert payload.attempt_count == 2


@pytest.mark.asyncio
async def test_retry_exhaustion_is_typed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("offline timeout", request=request)

    async def no_sleep(_: float) -> None:
        return None

    async with _client(tmp_path, httpx.MockTransport(handler), retries=1, sleep=no_sleep) as client:
        with pytest.raises(FredRequestError, match="exhausted retries"):
            await client.get_vintage_dates("GDPC1")


def test_expired_cache_entry_is_not_returned(tmp_path: Path) -> None:
    cache = FredFileCache(tmp_path, ttl_seconds=1)
    key = request_cache_key("series", {"series_id": "GDPC1"})
    cache.put(key, b"{}")
    path = tmp_path / f"{key}.json.gz"
    expired = time.time() - 5
    os.utime(path, (expired, expired))

    assert cache.get(key) is None
