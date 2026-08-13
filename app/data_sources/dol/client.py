"""Async client for the ETA/OUI National Weekly Claims XML report."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.data_sources.dol.exceptions import (
    DolOperationalDegradation,
    DolRequestError,
    DolResponseError,
)
from app.data_sources.dol.schemas import DolPayload
from app.data_sources.official.storage import (
    OfficialFileCache,
    OfficialRawStore,
    official_cache_key,
)

Sleep = Callable[[float], Awaitable[None]]
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
OPERATIONAL_MARKERS = (
    b"technical issue",
    b"maintenance",
    b"database unavailable",
    b"temporarily unavailable",
)


class DolClient:
    """Submit bounded National Weekly Claims XML report requests."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        max_retries: int,
        retry_base_seconds: float,
        cache: OfficialFileCache,
        raw_store: OfficialRawStore,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._cache = cache
        self._raw_store = raw_store
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "economic-cycle-monitor/0.6"},
        )

    async def __aenter__(self) -> "DolClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP transport."""
        await self._client.aclose()

    async def get_national_weekly_claims(self, start_year: int, end_year: int) -> DolPayload:
        """Fetch one bounded National r539cy XML response."""
        if not 1967 <= start_year <= end_year <= 2100:
            raise ValueError("DOL Weekly Claims years must be between 1967 and 2100")
        endpoint = "unemploy/wkclaims/report.asp"
        parameters = {
            "level": "us",
            "strtdate": str(start_year),
            "enddate": str(end_year),
            "filetype": "xml",
        }
        key = official_cache_key({"endpoint": endpoint, **parameters}, secret_names=set())
        started_at = datetime.now(UTC)
        cached = self._cache.get(key)
        if cached is not None:
            self._validate_xml_payload(cached, None)
            return self._payload(
                endpoint,
                parameters,
                cached,
                started_at,
                datetime.now(UTC),
                200,
                0,
                None,
                True,
                None,
            )
        for attempt in range(1, self._max_retries + 2):
            try:
                response = await self._client.post(endpoint, data=parameters)
            except httpx.RequestError as error:
                if attempt <= self._max_retries:
                    await self._sleep(self._retry_base_seconds * (2 ** (attempt - 1)))
                    continue
                raise DolRequestError(
                    "DOL request exhausted retries after a transport failure",
                    status_code=None,
                    attempt_count=attempt,
                ) from error
            if response.status_code in RETRYABLE_STATUS_CODES and attempt <= self._max_retries:
                await self._sleep(self._retry_base_seconds * (2 ** (attempt - 1)))
                continue
            if response.status_code >= 400:
                raise DolRequestError(
                    f"DOL request failed with HTTP {response.status_code}",
                    status_code=response.status_code,
                    attempt_count=attempt,
                )
            content_type = response.headers.get("content-type")
            self._validate_xml_payload(response.content, content_type)
            finished_at = datetime.now(UTC)
            digest, path = self._raw_store.archive(
                body=response.content, retrieved_at=finished_at, endpoint=endpoint
            )
            self._cache.put(key, response.content)
            return self._payload(
                endpoint,
                parameters,
                response.content,
                started_at,
                finished_at,
                response.status_code,
                attempt,
                path,
                False,
                content_type,
                digest,
            )
        raise AssertionError("DOL retry loop must return or raise")

    @staticmethod
    def _validate_xml_payload(body: bytes, content_type: str | None) -> None:
        if not body.strip():
            raise DolResponseError("DOL returned an empty Weekly Claims response")
        lowered = body.lower()
        if any(marker in lowered for marker in OPERATIONAL_MARKERS):
            raise DolOperationalDegradation(
                "DOL Weekly Claims source reports temporary operational degradation"
            )
        stripped = body.lstrip().lower()
        if stripped.startswith(b"<!doctype html") or stripped.startswith(b"<html"):
            raise DolResponseError("DOL returned HTML instead of Weekly Claims XML")
        if not stripped.startswith(b"<"):
            raise DolResponseError("DOL Weekly Claims response is not XML")
        if content_type and not any(
            token in content_type.lower() for token in ("xml", "octet-stream", "text/plain")
        ):
            raise DolResponseError("DOL Weekly Claims returned an unexpected content type")

    @staticmethod
    def _payload(
        endpoint: str,
        parameters: dict[str, str],
        body: bytes,
        started_at: datetime,
        finished_at: datetime,
        status_code: int,
        attempt_count: int,
        response_path: Path | None,
        from_cache: bool,
        content_type: str | None,
        digest: str | None = None,
    ) -> DolPayload:
        return DolPayload(
            endpoint=endpoint,
            sanitized_parameters=parameters,
            body=body,
            started_at=started_at,
            finished_at=finished_at,
            status_code=status_code,
            attempt_count=attempt_count,
            response_hash=digest or hashlib.sha256(body).hexdigest(),
            response_path=response_path,
            from_cache=from_cache,
            content_type=content_type,
        )
