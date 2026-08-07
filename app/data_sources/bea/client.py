"""Async, credential-safe client for the BEA Data API."""

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from app.data_sources.bea.exceptions import (
    BeaBusinessError,
    BeaRequestError,
    BeaResponseError,
)
from app.data_sources.bea.schemas import (
    BeaDataset,
    BeaNipaDataRow,
    BeaNote,
    BeaParameter,
    BeaParameterValue,
    BeaPayload,
)
from app.data_sources.official.storage import (
    OfficialFileCache,
    OfficialRawStore,
    official_cache_key,
)

Sleep = Callable[[float], Awaitable[None]]
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
SECRET_PARAMETER_NAMES = {"userid", "user_id", "api_key"}


class BeaClient:
    """Injectable JSON client for BEA metadata and bounded NIPA table requests."""

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
            headers={"User-Agent": "economic-cycle-monitor/0.4"},
        )

    async def __aenter__(self) -> "BeaClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def get_dataset_list(self) -> tuple[list[BeaDataset], BeaPayload]:
        """Return BEA datasets and safe request evidence."""
        body, payload = await self._request("GetDatasetList", {})
        values = self._result_list(body, "Dataset")
        try:
            return [BeaDataset.model_validate(item) for item in values], payload
        except ValidationError as error:
            raise BeaResponseError("BEA dataset-list response is invalid") from error

    async def get_parameter_list(
        self, dataset_name: str = "NIPA"
    ) -> tuple[list[BeaParameter], BeaPayload]:
        """Return request parameters supported by one BEA dataset."""
        body, payload = await self._request("GetParameterList", {"DataSetName": dataset_name})
        values = self._result_list(body, "Parameter")
        try:
            return [BeaParameter.model_validate(item) for item in values], payload
        except ValidationError as error:
            raise BeaResponseError("BEA parameter-list response is invalid") from error

    async def get_parameter_values(
        self,
        parameter_name: str,
        *,
        dataset_name: str = "NIPA",
        filters: Mapping[str, str] | None = None,
    ) -> tuple[list[BeaParameterValue], BeaPayload]:
        """Return permissible values for a BEA dataset parameter."""
        parameters: dict[str, str] = {
            "DataSetName": dataset_name,
            "ParameterName": parameter_name,
            **dict(filters or {}),
        }
        body, payload = await self._request("GetParameterValues", parameters)
        values = self._result_list(body, "ParamValue")
        try:
            return [BeaParameterValue.model_validate(item) for item in values], payload
        except ValidationError as error:
            raise BeaResponseError("BEA parameter-values response is invalid") from error

    async def get_nipa_data(
        self,
        *,
        table_name: str,
        frequency: str,
        years: list[int],
    ) -> tuple[list[BeaNipaDataRow], list[BeaNote], BeaPayload]:
        """Fetch one NIPA table for explicit years without using ALL."""
        normalized_frequency = frequency.upper()
        if normalized_frequency not in {"A", "Q", "M"}:
            raise ValueError("BEA frequency must be A, Q, or M")
        if not years or any(year < 1929 or year > 2100 for year in years):
            raise ValueError("BEA years must be an explicit non-empty bounded list")
        if not table_name or "," in table_name:
            raise ValueError("BEA GetData requires exactly one TableName")
        body, payload = await self._request(
            "GetData",
            {
                "DataSetName": "NIPA",
                "TableName": table_name,
                "Frequency": normalized_frequency,
                "Year": ",".join(str(year) for year in sorted(set(years))),
            },
        )
        try:
            rows = [BeaNipaDataRow.model_validate(item) for item in self._result_list(body, "Data")]
            notes = [BeaNote.model_validate(item) for item in self._result_list(body, "Notes")]
        except ValidationError as error:
            raise BeaResponseError("BEA NIPA data response is invalid") from error
        return rows, notes, payload

    async def _request(
        self, method: str, parameters: dict[str, str]
    ) -> tuple[dict[str, Any], BeaPayload]:
        safe_parameters: dict[str, str] = {
            "Method": method,
            "ResultFormat": "JSON",
            **parameters,
        }
        key = official_cache_key(safe_parameters, secret_names=SECRET_PARAMETER_NAMES)
        started_at = datetime.now(UTC)
        cached = self._cache.get(key)
        if cached is not None:
            return self._decode_payload(
                body=cached,
                method=method,
                safe_parameters=safe_parameters,
                started_at=started_at,
                status_code=200,
                attempt_count=0,
                from_cache=True,
                archive=False,
            )
        authenticated: dict[str, str] = dict(safe_parameters)
        if self._api_key is not None:
            authenticated["UserID"] = self._api_key.get_secret_value()
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 2):
            try:
                response = await self._client.get("", params=authenticated)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    if attempt <= self._max_retries:
                        await self._sleep(self._backoff(attempt, response))
                        continue
                    raise BeaRequestError(
                        f"BEA request exhausted retries with HTTP {response.status_code}",
                        status_code=response.status_code,
                        attempt_count=attempt,
                    )
                if response.is_error:
                    raise BeaRequestError(
                        f"BEA request failed with HTTP {response.status_code}",
                        status_code=response.status_code,
                        attempt_count=attempt,
                    )
                decoded, payload = self._decode_payload(
                    body=response.content,
                    method=method,
                    safe_parameters=safe_parameters,
                    started_at=started_at,
                    status_code=response.status_code,
                    attempt_count=attempt,
                    from_cache=False,
                    archive=True,
                )
                safe_body = json.dumps(
                    decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
                self._cache.put(key, safe_body)
                return decoded, payload
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                last_error = error
                if attempt <= self._max_retries:
                    await self._sleep(self._retry_base_seconds * 2 ** (attempt - 1))
                    continue
                break
        raise BeaRequestError(
            f"BEA request exhausted retries after {self._max_retries + 1} attempts",
            attempt_count=self._max_retries + 1,
        ) from last_error

    def _decode_payload(
        self,
        *,
        body: bytes,
        method: str,
        safe_parameters: dict[str, str],
        started_at: datetime,
        status_code: int,
        attempt_count: int,
        from_cache: bool,
        archive: bool,
    ) -> tuple[dict[str, Any], BeaPayload]:
        try:
            raw = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BeaResponseError("BEA returned invalid JSON") from error
        if not isinstance(raw, dict):
            raise BeaResponseError("BEA returned an invalid response envelope")
        decoded = self._redact(raw)
        error_payload = self._find_business_error(decoded)
        if error_payload is not None:
            code = str(error_payload.get("APIErrorCode") or "") or None
            description = str(
                error_payload.get("APIErrorDescription")
                or error_payload.get("Description")
                or "Unspecified BEA API error"
            )
            raise BeaBusinessError(code, description)
        self._results(decoded)
        finished_at = datetime.now(UTC)
        safe_body = json.dumps(
            decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        digest = hashlib.sha256(safe_body).hexdigest()
        path = None
        if archive:
            digest, path = self._raw_store.archive(
                body=safe_body,
                retrieved_at=finished_at,
                endpoint=method.lower(),
            )
        return decoded, BeaPayload(
            endpoint=method,
            sanitized_parameters=safe_parameters,
            body=decoded,
            started_at=started_at,
            finished_at=finished_at,
            status_code=status_code,
            attempt_count=attempt_count,
            response_hash=digest,
            response_path=path,
            from_cache=from_cache,
        )

    def _redact(self, value: Any) -> Any:
        secret = self._api_key.get_secret_value() if self._api_key is not None else None
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            is_user_id_parameter = str(value.get("ParameterName", "")).lower() == "userid"
            for key, item in value.items():
                if key.lower() in SECRET_PARAMETER_NAMES or (
                    is_user_id_parameter and key.lower() == "parametervalue"
                ):
                    redacted[key] = "***"
                else:
                    redacted[key] = self._redact(item)
            return redacted
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            result = value.replace(secret, "***") if secret else value
            return re.sub(r"(?i)(user\s*id|userid|api\s*key)(\s*[:=]?\s*)\S+", r"\1\2***", result)
        return value

    @staticmethod
    def _results(body: dict[str, Any]) -> dict[str, Any]:
        api = body.get("BEAAPI")
        if not isinstance(api, dict):
            raise BeaResponseError("BEA response is missing BEAAPI")
        results = api.get("Results")
        if isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict):
            return results[0]
        if not isinstance(results, dict):
            raise BeaResponseError("BEA response is missing Results")
        return results

    @classmethod
    def _result_list(cls, body: dict[str, Any], key: str) -> list[dict[str, Any]]:
        value = cls._results(body).get(key, [])
        if isinstance(value, dict):
            return [value]
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise BeaResponseError(f"BEA Results.{key} is invalid")
        return value

    @classmethod
    def _find_business_error(cls, body: dict[str, Any]) -> dict[str, Any] | None:
        api = body.get("BEAAPI")
        if not isinstance(api, dict):
            return None
        results = api.get("Results")
        candidates = results if isinstance(results, list) else [results]
        for candidate in candidates:
            if isinstance(candidate, dict):
                error = candidate.get("Error")
                if isinstance(error, list) and error and isinstance(error[0], dict):
                    return error[0]
                if isinstance(error, dict):
                    return error
        return None

    def _backoff(self, attempt: int, response: httpx.Response) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return self._retry_base_seconds * pow(2.0, attempt - 1)
