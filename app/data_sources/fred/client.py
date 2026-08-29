"""Asynchronous FRED client with retries, caching, and secret-free audit evidence."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

import httpx
from pydantic import SecretStr, TypeAdapter, ValidationError

from app.data_sources.fred.cache import FredFileCache, request_cache_key
from app.data_sources.fred.exceptions import (
    FredConfigurationError,
    FredRequestError,
    FredResponseError,
)
from app.data_sources.fred.raw_store import FredRawStore
from app.data_sources.fred.schemas import (
    FredObservation,
    FredPayload,
    FredRelease,
    FredSeriesMetadata,
)

Sleep = Callable[[float], Awaitable[None]]
AttemptGuard = Callable[[str, dict[str, str], int], None]
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def sanitize_parameters(parameters: dict[str, object]) -> dict[str, str]:
    """Serialize public request parameters and drop credentials."""
    return {
        key: value.isoformat() if isinstance(value, (date, datetime)) else str(value)
        for key, value in parameters.items()
        if value is not None and key.lower() != "api_key"
    }


class FredClient:
    """A narrow, injectable wrapper around the official FRED JSON endpoints."""

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        base_url: str,
        timeout_seconds: float,
        max_retries: int,
        retry_base_seconds: float,
        cache: FredFileCache,
        raw_store: FredRawStore,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        attempt_guard: AttemptGuard | None = None,
    ) -> None:
        self._api_key = api_key
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._cache = cache
        self._raw_store = raw_store
        self._sleep = sleep
        self._attempt_guard = attempt_guard
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "economic-cycle-monitor/0.2"},
        )

    async def __aenter__(self) -> "FredClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _authenticated_parameters(self, parameters: dict[str, object]) -> dict[str, str]:
        if self._api_key is None:
            raise FredConfigurationError("FRED_API_KEY is required for a live FRED request")
        safe = sanitize_parameters(parameters)
        return {**safe, "api_key": self._api_key.get_secret_value(), "file_type": "json"}

    async def request_json(
        self, endpoint: str, *, series_id: str | None = None, **parameters: object
    ) -> FredPayload:
        """Return one decoded response with secret-free request and raw-file evidence."""
        safe_parameters = sanitize_parameters(parameters)
        if series_id is not None:
            safe_parameters["series_id"] = series_id
        safe_parameters["file_type"] = "json"
        cache_key = request_cache_key(endpoint, safe_parameters)
        started_at = datetime.now(UTC)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return self._payload(
                endpoint=endpoint,
                series_id=series_id,
                parameters=safe_parameters,
                body=cached,
                started_at=started_at,
                status_code=200,
                attempt_count=0,
                from_cache=True,
                archive=False,
            )

        request_parameters: dict[str, object] = dict(parameters)
        if series_id is not None:
            request_parameters["series_id"] = series_id
        authenticated = self._authenticated_parameters(request_parameters)
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 2):
            try:
                if self._attempt_guard is not None:
                    self._attempt_guard(endpoint, safe_parameters, attempt)
                response = await self._client.get(endpoint.lstrip("/"), params=authenticated)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    if attempt <= self._max_retries:
                        await self._sleep(self._backoff(attempt, response))
                        continue
                    raise FredRequestError(
                        f"FRED request exhausted retries with HTTP {response.status_code}",
                        status_code=response.status_code,
                        attempt_count=attempt,
                    )
                if response.is_error:
                    raise FredRequestError(
                        f"FRED request failed with HTTP {response.status_code}",
                        status_code=response.status_code,
                        attempt_count=attempt,
                    )
                body = response.content
                self._cache.put(cache_key, body)
                return self._payload(
                    endpoint=endpoint,
                    series_id=series_id,
                    parameters=safe_parameters,
                    body=body,
                    started_at=started_at,
                    status_code=response.status_code,
                    attempt_count=attempt,
                    from_cache=False,
                    archive=True,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt <= self._max_retries:
                    await self._sleep(self._retry_base_seconds * 2 ** (attempt - 1))
                    continue
                break
        raise FredRequestError(
            f"FRED request exhausted retries after {self._max_retries + 1} attempts",
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

    def _payload(
        self,
        *,
        endpoint: str,
        series_id: str | None,
        parameters: dict[str, str],
        body: bytes,
        started_at: datetime,
        status_code: int,
        attempt_count: int,
        from_cache: bool,
        archive: bool,
    ) -> FredPayload:
        finished_at = datetime.now(UTC)
        try:
            decoded = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FredResponseError("FRED returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise FredResponseError("FRED JSON response must be an object")
        digest = hashlib.sha256(body).hexdigest()
        path = None
        if archive:
            digest, path = self._raw_store.archive(
                endpoint=endpoint,
                series_id=series_id,
                body=body,
                retrieved_at=finished_at,
            )
        return FredPayload(
            endpoint=endpoint,
            series_id=series_id,
            sanitized_parameters=parameters,
            body=decoded,
            started_at=started_at,
            finished_at=finished_at,
            status_code=status_code,
            attempt_count=attempt_count,
            response_hash=digest,
            response_path=path,
            from_cache=from_cache,
        )

    async def get_series_metadata(self, series_id: str) -> tuple[FredSeriesMetadata, FredPayload]:
        payload = await self.request_json("series", series_id=series_id)
        try:
            values = TypeAdapter(list[FredSeriesMetadata]).validate_python(payload.body["seriess"])
        except (KeyError, ValidationError) as exc:
            raise FredResponseError("FRED series response has no valid series metadata") from exc
        if len(values) != 1 or values[0].id != series_id:
            raise FredResponseError("FRED series response did not match the requested Series ID")
        return values[0], payload

    async def get_observations(
        self,
        series_id: str,
        *,
        observation_start: date | None = None,
        observation_end: date | None = None,
        realtime_start: date | None = None,
        realtime_end: date | None = None,
        limit: int = 100_000,
    ) -> tuple[list[FredObservation], FredPayload]:
        payload = await self.request_json(
            "series/observations",
            series_id=series_id,
            observation_start=observation_start,
            observation_end=observation_end,
            realtime_start=realtime_start,
            realtime_end=realtime_end,
            limit=limit,
            sort_order="asc",
        )
        try:
            values = TypeAdapter(list[FredObservation]).validate_python(
                payload.body["observations"]
            )
        except (KeyError, ValidationError) as exc:
            raise FredResponseError("FRED observations response is invalid") from exc
        return values, payload

    async def get_release(self, series_id: str) -> tuple[FredRelease, FredPayload]:
        payload = await self.request_json("series/release", series_id=series_id)
        try:
            values = TypeAdapter(list[FredRelease]).validate_python(payload.body["releases"])
        except (KeyError, ValidationError) as exc:
            raise FredResponseError("FRED release response is invalid") from exc
        if len(values) != 1:
            raise FredResponseError("FRED release response must contain one release")
        return values[0], payload

    async def get_vintage_dates(self, series_id: str) -> tuple[list[date], FredPayload]:
        payload = await self.request_json("series/vintagedates", series_id=series_id)
        try:
            values = TypeAdapter(list[date]).validate_python(payload.body["vintage_dates"])
        except (KeyError, ValidationError) as exc:
            raise FredResponseError("FRED vintage dates response is invalid") from exc
        return values, payload
