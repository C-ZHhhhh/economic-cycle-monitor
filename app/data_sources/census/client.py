"""Async, credential-safe client for the Census Economic Indicators API."""

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from app.data_sources.census.exceptions import (
    CensusBusinessError,
    CensusRequestError,
    CensusResponseError,
)
from app.data_sources.census.schemas import CensusPayload, CensusRow
from app.data_sources.official.storage import (
    OfficialFileCache,
    OfficialRawStore,
    official_cache_key,
)

Sleep = Callable[[float], Awaitable[None]]
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
SECRET_PARAMETER_NAMES = {"key", "api_key", "apikey"}
DATASET_PATTERN = re.compile(r"^[a-z0-9]+$")
TIME_PATTERN = re.compile(
    r"^(?:\d{4}(?:-\d{2})?|from \d{4}(?:-\d{2})?(?: to \d{4}(?:-\d{2})?)?|to \d{4}(?:-\d{2})?)$"
)
FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_DATA_FIELDS = (
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
)


class CensusClient:
    """Fetch bounded EITS metadata and data without exposing query credentials."""

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        base_url: str,
        timeout_seconds: float,
        max_retries: int,
        retry_base_seconds: float,
        cache: OfficialFileCache,
        raw_store: OfficialRawStore,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._api_key = api_key
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._cache = cache
        self._raw_store = raw_store
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "economic-cycle-monitor/0.5"},
        )

    async def __aenter__(self) -> "CensusClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def get_dataset_metadata(self, dataset: str) -> tuple[dict[str, Any], CensusPayload]:
        """Return the current discovery document for one allow-shaped dataset name."""
        normalized = self._validate_dataset(dataset)
        body, payload = await self._request(
            f"timeseries/eits/{normalized}.json", {}, require_rows=False
        )
        if not isinstance(body, dict):
            raise CensusResponseError("Census dataset metadata response is not an object")
        return body, payload

    async def get_variables_metadata(self, dataset: str) -> tuple[dict[str, Any], CensusPayload]:
        """Return variables metadata for one EITS dataset."""
        normalized = self._validate_dataset(dataset)
        body, payload = await self._request(
            f"timeseries/eits/{normalized}/variables.json", {}, require_rows=False
        )
        if not isinstance(body, dict) or not isinstance(body.get("variables"), dict):
            raise CensusResponseError("Census variables metadata omitted variables")
        return body, payload

    async def get_data(
        self,
        dataset: str,
        *,
        time: str,
        geography: str | None = "us:*",
        fields: Collection[str] = DEFAULT_DATA_FIELDS,
    ) -> tuple[list[CensusRow], CensusPayload]:
        """Return typed rows for one bounded time query without assuming row order."""
        normalized = self._validate_dataset(dataset)
        if not TIME_PATTERN.fullmatch(time):
            raise ValueError("Census time must be a bounded year/month or from/to expression")
        requested_fields = tuple(dict.fromkeys(fields))
        if not requested_fields or any(
            not FIELD_PATTERN.fullmatch(item) for item in requested_fields
        ):
            raise ValueError("Census get fields contain an invalid variable name")
        parameters: dict[str, str] = {"get": ",".join(requested_fields), "time": time}
        if geography is not None:
            if not geography or any(character in geography for character in "?&#"):
                raise ValueError("Census geography query is invalid")
            parameters["for"] = geography
        body, payload = await self._request(
            f"timeseries/eits/{normalized}", parameters, require_rows=True
        )
        rows = self._table_rows(body)
        try:
            return [CensusRow.model_validate(item) for item in rows], payload
        except ValidationError as error:
            raise CensusResponseError("Census data rows are missing required dimensions") from error

    @staticmethod
    def _validate_dataset(dataset: str) -> str:
        normalized = dataset.strip().lower()
        if not DATASET_PATTERN.fullmatch(normalized):
            raise ValueError("Census dataset must contain only lowercase letters and digits")
        return normalized

    @staticmethod
    def _table_rows(body: Any) -> list[dict[str, Any]]:
        if not isinstance(body, list) or len(body) < 2 or not isinstance(body[0], list):
            raise CensusResponseError("Census data response is empty or lacks a header row")
        header = body[0]
        if not header or any(not isinstance(item, str) for item in header):
            raise CensusResponseError("Census data header is invalid")
        rows: list[dict[str, Any]] = []
        for raw in body[1:]:
            if not isinstance(raw, list) or len(raw) != len(header):
                raise CensusResponseError("Census data row width does not match its header")
            rows.append(dict(zip(header, raw, strict=True)))
        if not rows:
            raise CensusResponseError("Census data response contains no observation rows")
        return rows

    async def _request(
        self,
        endpoint: str,
        parameters: dict[str, str],
        *,
        require_rows: bool,
    ) -> tuple[Any, CensusPayload]:
        safe_parameters = dict(parameters)
        cache_parameters: dict[str, object] = {"endpoint": endpoint, **safe_parameters}
        cache_key = official_cache_key(cache_parameters, secret_names=SECRET_PARAMETER_NAMES)
        started_at = datetime.now(UTC)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return self._decode_payload(
                body=cached,
                endpoint=endpoint,
                safe_parameters=safe_parameters,
                started_at=started_at,
                status_code=200,
                attempt_count=0,
                from_cache=True,
                archive=False,
                require_rows=require_rows,
            )
        authenticated = dict(safe_parameters)
        if self._api_key is not None:
            authenticated["key"] = self._api_key.get_secret_value()
        for attempt in range(1, self._max_retries + 2):
            try:
                response = await self._client.get(endpoint, params=authenticated)
            except httpx.RequestError as error:
                if attempt <= self._max_retries:
                    await self._sleep(self._backoff(attempt, None))
                    continue
                raise CensusRequestError(
                    "Census request exhausted retries after a transport failure",
                    status_code=None,
                    attempt_count=attempt,
                ) from error
            if response.status_code in RETRYABLE_STATUS_CODES and attempt <= self._max_retries:
                await self._sleep(self._backoff(attempt, response))
                continue
            if response.status_code >= 400:
                raise CensusRequestError(
                    f"Census request failed with HTTP {response.status_code}",
                    status_code=response.status_code,
                    attempt_count=attempt,
                )
            sanitized_body = self._redact_secret_bytes(response.content)
            decoded, payload = self._decode_payload(
                body=sanitized_body,
                endpoint=endpoint,
                safe_parameters=safe_parameters,
                started_at=started_at,
                status_code=response.status_code,
                attempt_count=attempt,
                from_cache=False,
                archive=True,
                require_rows=require_rows,
            )
            self._cache.put(cache_key, sanitized_body)
            return decoded, payload
        raise AssertionError("Census retry loop must return or raise")

    def _decode_payload(
        self,
        *,
        body: bytes,
        endpoint: str,
        safe_parameters: dict[str, str],
        started_at: datetime,
        status_code: int,
        attempt_count: int,
        from_cache: bool,
        archive: bool,
        require_rows: bool,
    ) -> tuple[Any, CensusPayload]:
        stripped = body.lstrip()
        if stripped.startswith(b"<"):
            raise CensusResponseError("Census returned an HTML response instead of JSON")
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CensusResponseError("Census returned invalid JSON") from error
        self._raise_business_error(decoded)
        if require_rows and (not isinstance(decoded, list) or len(decoded) < 2):
            raise CensusResponseError("Census data response is empty")
        finished_at = datetime.now(UTC)
        digest = hashlib.sha256(body).hexdigest()
        response_path = None
        if archive:
            digest, response_path = self._raw_store.archive(
                body=body, retrieved_at=finished_at, endpoint=endpoint
            )
        payload = CensusPayload(
            endpoint=endpoint,
            sanitized_parameters=safe_parameters,
            body=decoded,
            started_at=started_at,
            finished_at=finished_at,
            status_code=status_code,
            attempt_count=attempt_count,
            response_hash=digest,
            response_path=response_path,
            from_cache=from_cache,
        )
        return decoded, payload

    @staticmethod
    def _raise_business_error(decoded: Any) -> None:
        message: str | None = None
        if isinstance(decoded, dict):
            for key in ("error", "Error", "message", "Message"):
                if decoded.get(key):
                    message = str(decoded[key])
                    break
        elif (
            isinstance(decoded, list)
            and decoded
            and isinstance(decoded[0], str)
            and "error" in decoded[0].lower()
        ):
            message = decoded[0]
        if message is not None:
            raise CensusBusinessError(f"Census business error: {message}")

    def _redact_secret_bytes(self, body: bytes) -> bytes:
        if self._api_key is None:
            return body
        secret = self._api_key.get_secret_value().encode()
        return body.replace(secret, b"***")

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            raw = response.headers.get("Retry-After")
            if raw is not None:
                try:
                    parsed = float(raw)
                    return max(0.0, parsed)
                except ValueError:
                    pass
        return float(self._retry_base_seconds * (2 ** (attempt - 1)))
