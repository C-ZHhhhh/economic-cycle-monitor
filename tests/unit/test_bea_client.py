"""Offline transport tests for the credential-safe BEA client."""

import gzip
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.data_sources.bea.client import BeaClient
from app.data_sources.bea.exceptions import BeaBusinessError, BeaRequestError
from app.data_sources.official.storage import OfficialFileCache, OfficialRawStore

TEST_KEY = "test-bea-key-not-real"


def _client(
    tmp_path: Path,
    transport: httpx.AsyncBaseTransport,
    *,
    max_retries: int = 0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> BeaClient:
    return BeaClient(
        api_key=SecretStr(TEST_KEY),
        base_url="https://example.test/api/data",
        timeout_seconds=1,
        max_retries=max_retries,
        retry_base_seconds=0,
        cache=OfficialFileCache(tmp_path / "cache", ttl_seconds=3600),
        raw_store=OfficialRawStore(tmp_path / "raw"),
        transport=transport,
        **({"sleep": sleep} if sleep is not None else {}),
    )


def _envelope(key: str, value: object) -> dict[str, object]:
    return {
        "BEAAPI": {
            "Request": {"RequestParam": [{"ParameterName": "USERID", "ParameterValue": TEST_KEY}]},
            "Results": {key: value},
        }
    }


@pytest.mark.asyncio
async def test_metadata_methods_parse_and_redact_key(tmp_path: Path) -> None:
    responses = {
        "GetDatasetList": _envelope(
            "Dataset", [{"DatasetName": "NIPA", "DatasetDescription": "Standard NIPA"}]
        ),
        "GetParameterList": _envelope(
            "Parameter", [{"ParameterName": "TableName", "ParameterDescription": "Table"}]
        ),
        "GetParameterValues": _envelope("ParamValue", [{"Key": "T10106", "Desc": "Real GDP"}]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["UserID"] == TEST_KEY
        return httpx.Response(200, json=responses[request.url.params["Method"]])

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        datasets, dataset_payload = await client.get_dataset_list()
        parameters, _ = await client.get_parameter_list()
        values, _ = await client.get_parameter_values("TableName")

    assert datasets[0].DatasetName == "NIPA"
    assert parameters[0].ParameterName == "TableName"
    assert values[0].Key == "T10106"
    assert TEST_KEY not in dataset_payload.model_dump_json()
    archived = list((tmp_path / "raw").rglob("*.gz"))
    assert len(archived) == 3
    for path in archived:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            assert TEST_KEY not in handle.read()


@pytest.mark.asyncio
async def test_get_data_uses_one_table_explicit_years_and_parses_rows(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["TableName"] == "T10106"
        assert request.url.params["Frequency"] == "Q"
        assert request.url.params["Year"] == "2024,2025"
        return httpx.Response(
            200,
            json=_envelope(
                "Data",
                [
                    {
                        "TableName": "T10106",
                        "SeriesCode": "A191RX",
                        "LineNumber": "1",
                        "LineDescription": "Gross domestic product",
                        "TimePeriod": "2025Q1",
                        "METRIC_NAME": "Chained Dollars",
                        "CL_UNIT": "Billions of Chained 2017 Dollars",
                        "UNIT_MULT": "9",
                        "DataValue": "23,548.210",
                        "NoteRef": "T10106",
                    }
                ],
            ),
        )

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        rows, notes, payload = await client.get_nipa_data(
            table_name="T10106", frequency="Q", years=[2025, 2024]
        )
    assert rows[0].SeriesCode == "A191RX"
    assert notes == []
    assert "UserID" not in payload.sanitized_parameters


@pytest.mark.asyncio
async def test_http_200_business_error_is_failed_and_not_archived(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "BEAAPI": {
                    "Results": {
                        "Error": {
                            "APIErrorCode": "4",
                            "APIErrorDescription": f"Invalid UserID {TEST_KEY}",
                        }
                    }
                }
            },
        )

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        with pytest.raises(BeaBusinessError) as captured:
            await client.get_dataset_list()
    assert TEST_KEY not in str(captured.value)
    assert "***" in str(captured.value)
    assert not list((tmp_path / "raw").rglob("*.gz"))
    assert not list((tmp_path / "cache").rglob("*.gz"))


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable_status", [429, 500])
async def test_retryable_http_status_retries_then_succeeds(
    tmp_path: Path, retryable_status: int
) -> None:
    attempts = 0
    sleeps: list[float] = []

    async def no_sleep(value: float) -> None:
        sleeps.append(value)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(retryable_status, headers={"Retry-After": "0"})
        return httpx.Response(200, json=_envelope("Dataset", []))

    async with _client(
        tmp_path,
        httpx.MockTransport(handler),
        max_retries=1,
        sleep=no_sleep,
    ) as client:
        await client.get_dataset_list()
    assert attempts == 2
    assert sleeps == [0]


@pytest.mark.asyncio
async def test_timeout_exhaustion_is_safe(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("safe timeout", request=request)

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        with pytest.raises(BeaRequestError, match="exhausted retries"):
            await client.get_dataset_list()


@pytest.mark.asyncio
async def test_successful_second_call_hits_cache(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json=_envelope("Dataset", []))

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        _, first = await client.get_dataset_list()
        _, second = await client.get_dataset_list()
    assert attempts == 1
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.attempt_count == 0


@pytest.mark.asyncio
async def test_get_data_rejects_all_and_multi_table_shapes(tmp_path: Path) -> None:
    async with _client(
        tmp_path, httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        with pytest.raises(ValueError, match="explicit"):
            await client.get_nipa_data(table_name="T10106", frequency="Q", years=[])
        with pytest.raises(ValueError, match="exactly one"):
            await client.get_nipa_data(table_name="T10106,T20804", frequency="Q", years=[2025])


def test_cached_payload_contains_valid_sanitized_json(tmp_path: Path) -> None:
    cache = OfficialFileCache(tmp_path, ttl_seconds=3600)
    cache.put("key", json.dumps({"safe": True}).encode())
    assert json.loads(cache.get("key") or b"{}") == {"safe": True}
