"""Async client for stable Federal Reserve Board release files."""

import asyncio
import hashlib
import io
import zipfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import httpx

from app.data_sources.federal_reserve.exceptions import (
    FederalReserveRequestError,
    FederalReserveResponseError,
)
from app.data_sources.federal_reserve.schemas import FederalReservePayload
from app.data_sources.official.storage import (
    OfficialFileCache,
    OfficialRawStore,
    official_cache_key,
)

Sleep = Callable[[float], Awaitable[None]]
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
FORBIDDEN_RUNTIME_PATH_TOKENS = ("datadownload", "build.aspx", "choose.aspx", "download.aspx")


class FederalReserveClient:
    """Fetch Board-owned static text, release HTML, and full XML ZIP assets."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        max_retries: int,
        retry_base_seconds: float,
        cache_root: Path,
        raw_root: Path,
        cache_ttl_seconds: int,
        cache_enabled: bool,
        zip_max_members: int = 128,
        zip_max_uncompressed_bytes: int = 64 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._cache_root = cache_root
        self._raw_root = raw_root
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_enabled = cache_enabled
        self._zip_max_members = zip_max_members
        self._zip_max_uncompressed_bytes = zip_max_uncompressed_bytes
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "economic-cycle-monitor/0.6"},
        )

    async def __aenter__(self) -> "FederalReserveClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP transport."""
        await self._client.aclose()

    async def get_text(self, path: str) -> FederalReservePayload:
        """Fetch one official plain-text asset."""
        return await self._request(path, suffix="txt.gz", expected="text")

    async def get_html(self, path: str) -> FederalReservePayload:
        """Fetch one official release metadata page."""
        return await self._request(path, suffix="txt.gz", expected="html")

    async def get_xml_zip(self, path: str) -> FederalReservePayload:
        """Fetch one official complete release XML ZIP."""
        return await self._request(path, suffix="zip", expected="zip")

    async def _request(self, path: str, *, suffix: str, expected: str) -> FederalReservePayload:
        endpoint = self._validate_path(path)
        key = official_cache_key(
            {"provider": "federal_reserve_board", "endpoint": endpoint}, secret_names=set()
        )
        cache = OfficialFileCache(
            self._cache_root,
            ttl_seconds=self._cache_ttl_seconds,
            enabled=self._cache_enabled,
            suffix=suffix,
        )
        raw_store = OfficialRawStore(self._raw_root, suffix=suffix)
        started_at = datetime.now(UTC)
        cached = cache.get(key)
        if cached is not None:
            self._validate_body(cached, expected=expected, content_type=None)
            return self._payload(
                endpoint,
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
                response = await self._client.get(endpoint)
            except httpx.RequestError as error:
                if attempt <= self._max_retries:
                    await self._sleep(self._retry_base_seconds * (2 ** (attempt - 1)))
                    continue
                raise FederalReserveRequestError(
                    "Federal Reserve request exhausted retries after a transport failure",
                    status_code=None,
                    attempt_count=attempt,
                ) from error
            if response.status_code in RETRYABLE_STATUS_CODES and attempt <= self._max_retries:
                await self._sleep(self._retry_base_seconds * (2 ** (attempt - 1)))
                continue
            if response.status_code >= 400:
                raise FederalReserveRequestError(
                    f"Federal Reserve request failed with HTTP {response.status_code}",
                    status_code=response.status_code,
                    attempt_count=attempt,
                )
            content_type = response.headers.get("content-type")
            self._validate_body(response.content, expected=expected, content_type=content_type)
            finished_at = datetime.now(UTC)
            digest, archived = raw_store.archive(
                body=response.content, retrieved_at=finished_at, endpoint=endpoint
            )
            cache.put(key, response.content)
            return self._payload(
                endpoint,
                response.content,
                started_at,
                finished_at,
                response.status_code,
                attempt,
                archived,
                False,
                content_type,
                digest,
            )
        raise AssertionError("Federal Reserve retry loop must return or raise")

    @staticmethod
    def _validate_path(path: str) -> str:
        normalized = path.strip().lstrip("/")
        lowered = normalized.lower()
        if not normalized or any(token in lowered for token in FORBIDDEN_RUNTIME_PATH_TOKENS):
            raise ValueError("Federal Reserve production runtime forbids DDP package paths")
        if ".." in normalized or "?" in normalized or "#" in normalized:
            raise ValueError("Federal Reserve release path is invalid")
        return normalized

    def _validate_body(self, body: bytes, *, expected: str, content_type: str | None) -> None:
        if not body:
            raise FederalReserveResponseError("Federal Reserve returned an empty payload")
        stripped = body.lstrip().lower()
        is_html = stripped.startswith(b"<!doctype html") or stripped.startswith(b"<html")
        if is_html and expected != "html":
            raise FederalReserveResponseError(
                "Federal Reserve returned HTML instead of the requested release asset"
            )
        if expected == "html" and not is_html:
            raise FederalReserveResponseError("Federal Reserve metadata page is not HTML")
        if expected == "zip" and not body.startswith(b"PK\x03\x04"):
            raise FederalReserveResponseError("Federal Reserve XML download is not a valid ZIP")
        if expected == "zip" and not zipfile.is_zipfile(io.BytesIO(body)):
            raise FederalReserveResponseError("Federal Reserve XML download is a truncated ZIP")
        if expected == "zip":
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                members = archive.infolist()
                if not members or len(members) > self._zip_max_members:
                    raise FederalReserveResponseError("Federal Reserve ZIP member limit exceeded")
                if sum(item.file_size for item in members) > self._zip_max_uncompressed_bytes:
                    raise FederalReserveResponseError(
                        "Federal Reserve ZIP uncompressed size limit exceeded"
                    )
                if any(
                    PurePosixPath(item.filename).is_absolute()
                    or ".." in PurePosixPath(item.filename).parts
                    for item in members
                ):
                    raise FederalReserveResponseError(
                        "Federal Reserve ZIP contains an unsafe member path"
                    )
        if (
            content_type
            and expected == "zip"
            and not any(token in content_type.lower() for token in ("zip", "octet-stream"))
        ):
            raise FederalReserveResponseError(
                "Federal Reserve XML ZIP returned an unexpected content type"
            )

    @staticmethod
    def _payload(
        endpoint: str,
        body: bytes,
        started_at: datetime,
        finished_at: datetime,
        status_code: int,
        attempt_count: int,
        response_path: Path | None,
        from_cache: bool,
        content_type: str | None,
        digest: str | None = None,
    ) -> FederalReservePayload:
        return FederalReservePayload(
            endpoint=endpoint,
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
