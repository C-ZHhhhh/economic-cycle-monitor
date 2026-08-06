"""Offline-only tests for BLS batching, retries, caching, and adaptation."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.data_sources.bls.adapter import BlsAdapter
from app.data_sources.bls.cache import BlsFileCache
from app.data_sources.bls.client import BlsClient, split_year_ranges
from app.data_sources.bls.exceptions import BlsBusinessError, BlsRequestError
from app.data_sources.bls.factory import create_bls_client
from app.data_sources.bls.raw_store import BlsRawStore
from app.settings import Settings

TEST_CREDENTIAL = "offline-test-registration-credential"


def _response(
    series_ids: list[str],
    *,
    status: str = "REQUEST_SUCCEEDED",
    messages: list[str] | None = None,
    data: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    points = data or [
        {
            "year": "2025",
            "period": "M01",
            "periodName": "January",
            "value": "4.0",
            "footnotes": [{}],
        }
    ]
    return {
        "status": status,
        "responseTime": 1,
        "message": messages or [],
        "Results": {
            "series": [{"seriesID": series_id, "data": points} for series_id in series_ids]
        },
    }


def _client(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> BlsClient:
    async def no_sleep(_: float) -> None:
        return None

    return BlsClient(
        api_key=SecretStr(TEST_CREDENTIAL),
        base_url="https://offline.bls.test/publicAPI/v2",
        timeout_seconds=1,
        max_retries=max_retries,
        retry_base_seconds=0,
        max_series_per_request=50,
        max_years_per_request=20,
        cache=BlsFileCache(tmp_path / "cache", ttl_seconds=3600),
        raw_store=BlsRawStore(tmp_path / "raw"),
        transport=httpx.MockTransport(handler),
        sleep=sleep or no_sleep,
    )


@pytest.mark.asyncio
async def test_batch_request_is_secret_free_outside_transport(tmp_path: Path) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=_response(["SERIES_A", "SERIES_B"]))

    async with _client(tmp_path, handler) as client:
        series, payload = await client.get_series_data(
            ["SERIES_A", "SERIES_B"], start_year=2020, end_year=2025
        )
    assert [item.seriesID for item in series] == ["SERIES_A", "SERIES_B"]
    assert requests[0]["registrationkey"] == TEST_CREDENTIAL
    rendered = payload.model_dump_json()
    assert TEST_CREDENTIAL not in rendered
    assert "registrationkey" not in rendered.lower()
    assert payload.response_path is not None
    assert TEST_CREDENTIAL.encode() not in payload.response_path.read_bytes()


@pytest.mark.asyncio
async def test_adapter_preserves_missing_preliminary_and_ignores_annual_average(
    tmp_path: Path,
) -> None:
    data: list[dict[str, object]] = [
        {
            "year": "2025",
            "period": "M02",
            "periodName": "February",
            "value": ".",
            "footnotes": [{"code": "P", "text": "Preliminary"}],
        },
        {
            "year": "2025",
            "period": "M13",
            "periodName": "Annual",
            "value": "99",
            "footnotes": [{}],
        },
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response(["SERIES_A"], data=data))

    async with _client(tmp_path, handler) as client:
        series, _ = await client.get_series_data(["SERIES_A"], start_year=2025, end_year=2025)
    observations = BlsAdapter.observations(series[0])
    assert len(observations) == 1
    assert observations[0].value is None
    assert observations[0].is_preliminary is True
    assert observations[0].footnotes == [{"code": "P", "text": "Preliminary"}]


@pytest.mark.asyncio
async def test_cache_hit_avoids_second_transport_call(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_response(["SERIES_A"]))

    async with _client(tmp_path, handler) as client:
        first = await client.get_series_data(["SERIES_A"], start_year=2025, end_year=2025)
        second = await client.get_series_data(["SERIES_A"], start_year=2025, end_year=2025)
    assert calls == 1
    assert first[1].from_cache is False
    assert second[1].from_cache is True
    assert second[1].attempt_count == 0


@pytest.mark.asyncio
async def test_http_success_business_failure_is_rejected(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=_response(
                [],
                status="REQUEST_FAILED",
                messages=[f"Invalid registration key: {TEST_CREDENTIAL}"],
            ),
        )

    async with _client(tmp_path, handler) as client:
        with pytest.raises(BlsBusinessError, match="REQUEST_FAILED") as first:
            await client.get_series_data(["SERIES_A"], start_year=2025, end_year=2025)
        with pytest.raises(BlsBusinessError, match="REQUEST_FAILED"):
            await client.get_series_data(["SERIES_A"], start_year=2025, end_year=2025)
    assert calls == 2
    assert first.value.messages == ["Invalid registration key: ***"]
    assert TEST_CREDENTIAL not in str(first.value)
    assert not (tmp_path / "cache").exists()
    assert not (tmp_path / "raw").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_status", [429, 503])
async def test_retryable_http_status_is_retried(tmp_path: Path, first_status: int) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(first_status)
        return httpx.Response(200, json=_response(["SERIES_A"]))

    async with _client(tmp_path, handler, max_retries=1) as client:
        await client.get_series_data(["SERIES_A"], start_year=2025, end_year=2025)
    assert calls == 2


@pytest.mark.asyncio
async def test_timeout_exhaustion_raises_typed_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("offline timeout", request=request)

    async with _client(tmp_path, handler, max_retries=1) as client:
        with pytest.raises(BlsRequestError, match="exhausted retries") as error:
            await client.get_series_data(["SERIES_A"], start_year=2025, end_year=2025)
    assert error.value.attempt_count == 2


@pytest.mark.asyncio
async def test_client_enforces_batch_and_year_limits(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called")

    async with _client(tmp_path, handler) as client:
        client.max_series_per_request = 1
        with pytest.raises(ValueError, match="1-1 Series IDs"):
            await client.get_series_data(["A", "B"], start_year=2025, end_year=2025)
        client.max_years_per_request = 2
        with pytest.raises(ValueError, match="2-year limit"):
            await client.get_series_data(["A"], start_year=2020, end_year=2025)


def test_split_year_ranges_uses_inclusive_provider_limits() -> None:
    assert split_year_ranges(2000, 2026, 20) == [(2000, 2019), (2020, 2026)]


@pytest.mark.asyncio
async def test_factory_uses_public_and_registered_bls_limits(tmp_path: Path) -> None:
    public = create_bls_client(
        Settings(_env_file=None, cache_dir=tmp_path / "cache", raw_data_dir=tmp_path / "raw")
    )
    registered = create_bls_client(
        Settings(
            _env_file=None,
            bls_api_key=SecretStr(TEST_CREDENTIAL),
            cache_dir=tmp_path / "cache",
            raw_data_dir=tmp_path / "raw",
        )
    )
    try:
        assert (public.max_series_per_request, public.max_years_per_request) == (25, 10)
        assert (registered.max_series_per_request, registered.max_years_per_request) == (50, 20)
    finally:
        await public.aclose()
        await registered.aclose()
