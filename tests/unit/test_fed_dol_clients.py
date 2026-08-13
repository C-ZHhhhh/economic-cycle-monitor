"""Offline transport, cache, payload-shape, and degradation tests."""

from pathlib import Path

import httpx
import pytest

from app.data_sources.dol.client import DolClient
from app.data_sources.dol.exceptions import DolOperationalDegradation, DolResponseError
from app.data_sources.federal_reserve.client import FederalReserveClient
from app.data_sources.federal_reserve.exceptions import FederalReserveResponseError
from app.data_sources.official.storage import OfficialFileCache, OfficialRawStore

pytestmark = pytest.mark.asyncio


def _fed(tmp_path: Path, handler: httpx.MockTransport) -> FederalReserveClient:
    return FederalReserveClient(
        base_url="https://offline.federalreserve.test",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        cache_root=tmp_path / "cache",
        raw_root=tmp_path / "raw",
        cache_ttl_seconds=3600,
        cache_enabled=True,
        transport=handler,
    )


def _dol(tmp_path: Path, handler: httpx.MockTransport) -> DolClient:
    return DolClient(
        base_url="https://offline.dol.test",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        cache=OfficialFileCache(
            tmp_path / "cache", ttl_seconds=3600, enabled=True, suffix="xml.gz"
        ),
        raw_store=OfficialRawStore(tmp_path / "raw", suffix="xml.gz"),
        transport=handler,
    )


async def test_fed_runtime_rejects_ddp_and_does_not_need_package_identifier(tmp_path: Path) -> None:
    async with _fed(
        tmp_path, httpx.MockTransport(lambda _: httpx.Response(200, text="2025 1 2"))
    ) as client:
        with pytest.raises(ValueError, match="forbids DDP"):
            await client.get_text("datadownload/Build.aspx")
        payload = await client.get_text("releases/g17/ipdisk/ip_sa.txt")
    assert payload.endpoint == "releases/g17/ipdisk/ip_sa.txt"


async def test_fed_html_error_and_truncated_zip_fail_on_http_200(tmp_path: Path) -> None:
    async with _fed(
        tmp_path,
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"<html>maintenance</html>")),
    ) as client:
        with pytest.raises(FederalReserveResponseError, match="HTML"):
            await client.get_text("releases/g17/ipdisk/ip_sa.txt")
    async with _fed(
        tmp_path / "two",
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"PK\x03\x04bad")),
    ) as client:
        with pytest.raises(FederalReserveResponseError, match="truncated ZIP"):
            await client.get_xml_zip("data/g19/g19_xml.zip")


async def test_dol_operational_degradation_is_distinct_from_identity_failure(
    tmp_path: Path,
) -> None:
    async with _dol(
        tmp_path,
        httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"<notice>database unavailable</notice>")
        ),
    ) as client:
        with pytest.raises(DolOperationalDegradation):
            await client.get_national_weekly_claims(2025, 2026)


async def test_dol_html_and_empty_payloads_fail(tmp_path: Path) -> None:
    for body in (b"", b"<html>error</html>"):
        async with _dol(
            tmp_path / str(len(body)),
            httpx.MockTransport(lambda _, value=body: httpx.Response(200, content=value)),
        ) as client:
            with pytest.raises(DolResponseError):
                await client.get_national_weekly_claims(2025, 2026)


async def test_fed_retryable_status_recovers_without_dynamic_package(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, text="2025 1 2")

    client = FederalReserveClient(
        base_url="https://offline.federalreserve.test",
        timeout_seconds=1,
        max_retries=1,
        retry_base_seconds=0,
        cache_root=tmp_path / "cache",
        raw_root=tmp_path / "raw",
        cache_ttl_seconds=3600,
        cache_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    async with client:
        payload = await client.get_text("releases/g17/ipdisk/ip_sa.txt")
    assert payload.status_code == 200
    assert calls == 2


async def test_fed_second_identical_request_hits_common_cache(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="2025 1 2")

    async with _fed(tmp_path, httpx.MockTransport(handler)) as client:
        first = await client.get_text("releases/g17/ipdisk/ip_sa.txt")
        second = await client.get_text("releases/g17/ipdisk/ip_sa.txt")
    assert first.from_cache is False
    assert second.from_cache is True
    assert calls == 1


async def test_fed_empty_payload_fails_on_http_200(tmp_path: Path) -> None:
    async with _fed(
        tmp_path, httpx.MockTransport(lambda _: httpx.Response(200, content=b""))
    ) as client:
        with pytest.raises(FederalReserveResponseError, match="empty payload"):
            await client.get_text("releases/g17/ipdisk/ip_sa.txt")


async def test_dol_second_identical_request_hits_cache(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "POST"
        assert b"filetype=xml" in request.content
        return httpx.Response(200, content=b"<report />", headers={"content-type": "text/xml"})

    async with _dol(tmp_path, httpx.MockTransport(handler)) as client:
        first = await client.get_national_weekly_claims(2025, 2026)
        second = await client.get_national_weekly_claims(2025, 2026)
    assert first.from_cache is False
    assert second.from_cache is True
    assert calls == 1
