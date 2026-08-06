"""Async BLS Public Data API v2 client with safe batch requests."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr, ValidationError

from app.data_sources.bls.cache import BlsFileCache, bls_cache_key
from app.data_sources.bls.exceptions import (
    BlsBusinessError,
    BlsRequestError,
    BlsResponseError,
)
from app.data_sources.bls.raw_store import BlsRawStore
from app.data_sources.bls.schemas import BlsApiResponse, BlsPayload, BlsSeries

Sleep = Callable[[float], Awaitable[None]]
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def split_year_ranges(start_year: int, end_year: int, max_years: int) -> list[tuple[int, int]]:
    """Split an inclusive range into provider-compliant inclusive windows."""
    if start_year > end_year:
        raise ValueError("start_year must not be after end_year")
    if max_years < 1:
        raise ValueError("max_years must be positive")
    ranges: list[tuple[int, int]] = []
    current = start_year
    while current <= end_year:
        window_end = min(current + max_years - 1, end_year)
        ranges.append((current, window_end))
        current = window_end + 1
    return ranges


class BlsClient:
    """Injectable POST-only wrapper around BLS time-series data v2."""

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        base_url: str,
        timeout_seconds: float,
        max_retries: int,
        retry_base_seconds: float,
        max_series_per_request: int,
        max_years_per_request: int,
        cache: BlsFileCache,
        raw_store: BlsRawStore,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._api_key = api_key
        self.max_series_per_request = max_series_per_request
        self.max_years_per_request = max_years_per_request
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._cache = cache
        self._raw_store = raw_store
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "economic-cycle-monitor/0.3"},
        )

    async def __aenter__(self) -> "BlsClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_series_data(
        self,
        series_ids: list[str],
        *,
        start_year: int,
        end_year: int,
        catalog: bool = False,
        annual_average: bool = False,
        calculations: bool = False,
        aspects: bool = False,
    ) -> tuple[list[BlsSeries], BlsPayload]:
        """Fetch a bounded batch and reject HTTP-success business failures."""
        if not series_ids or len(series_ids) > self.max_series_per_request:
            raise ValueError(f"BLS request must contain 1-{self.max_series_per_request} Series IDs")
        if end_year - start_year + 1 > self.max_years_per_request:
            raise ValueError(
                f"BLS request exceeds the configured {self.max_years_per_request}-year limit"
            )
        safe_parameters: dict[str, object] = {
            "seriesid": list(series_ids),
            "startyear": str(start_year),
            "endyear": str(end_year),
            "catalog": catalog,
            "annualaverage": annual_average,
            "calculations": calculations,
            "aspects": aspects,
        }
        cache_key = bls_cache_key(safe_parameters)
        started_at = datetime.now(UTC)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return self._decode_payload(
                body=cached,
                series_ids=series_ids,
                safe_parameters=safe_parameters,
                started_at=started_at,
                status_code=200,
                attempt_count=0,
                from_cache=True,
                archive=False,
            )
        authenticated = dict(safe_parameters)
        if self._api_key is not None:
            authenticated["registrationkey"] = self._api_key.get_secret_value()
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 2):
            try:
                response = await self._client.post("timeseries/data/", json=authenticated)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    if attempt <= self._max_retries:
                        await self._sleep(self._backoff(attempt, response))
                        continue
                    raise BlsRequestError(
                        f"BLS request exhausted retries with HTTP {response.status_code}",
                        status_code=response.status_code,
                        attempt_count=attempt,
                    )
                if response.is_error:
                    raise BlsRequestError(
                        f"BLS request failed with HTTP {response.status_code}",
                        status_code=response.status_code,
                        attempt_count=attempt,
                    )
                body = response.content
                result = self._decode_payload(
                    body=body,
                    series_ids=series_ids,
                    safe_parameters=safe_parameters,
                    started_at=started_at,
                    status_code=response.status_code,
                    attempt_count=attempt,
                    from_cache=False,
                    archive=True,
                )
                self._cache.put(cache_key, body)
                return result
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                last_error = error
                if attempt <= self._max_retries:
                    await self._sleep(self._retry_base_seconds * 2 ** (attempt - 1))
                    continue
                break
        raise BlsRequestError(
            f"BLS request exhausted retries after {self._max_retries + 1} attempts",
            attempt_count=self._max_retries + 1,
        ) from last_error

    def _backoff(self, attempt: int, response: httpx.Response) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return self._retry_base_seconds * pow(2.0, attempt - 1)

    def _decode_payload(
        self,
        *,
        body: bytes,
        series_ids: list[str],
        safe_parameters: dict[str, object],
        started_at: datetime,
        status_code: int,
        attempt_count: int,
        from_cache: bool,
        archive: bool,
    ) -> tuple[list[BlsSeries], BlsPayload]:
        finished_at = datetime.now(UTC)
        try:
            decoded = json.loads(body)
            response = BlsApiResponse.model_validate(decoded)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
            raise BlsResponseError("BLS returned an invalid response envelope") from error
        if response.status != "REQUEST_SUCCEEDED":
            raise BlsBusinessError(response.status)
        digest = hashlib.sha256(body).hexdigest()
        path = None
        if archive:
            digest, path = self._raw_store.archive(body=body, retrieved_at=finished_at)
        payload = BlsPayload(
            endpoint="timeseries/data/",
            series_ids=list(series_ids),
            sanitized_parameters=safe_parameters,
            body=decoded,
            started_at=started_at,
            finished_at=finished_at,
            status_code=status_code,
            attempt_count=attempt_count,
            response_hash=digest,
            response_path=path,
            from_cache=from_cache,
            business_messages=response.message,
        )
        return response.Results.series, payload
