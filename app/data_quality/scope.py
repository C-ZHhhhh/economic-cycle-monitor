"""Typed runtime scope and legacy audit classification for DLCR-002."""

from datetime import date
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import DataQualityEvent, DataSourceRequest, SyncRun

CLASSIFICATION_VERSION = "dlcr-002-v1"
AUDIT_QUALITY_SCOPE = "_quality_scope"
AUDIT_RUNTIME_DOMAIN = "_runtime_domain"
AUDIT_PIT_STATUS = "_pit_availability_status"
AUDIT_RUNTIME_BLOCKING = "_runtime_blocking"


class QualityScope(StrEnum):
    """Operational evaluation scope, independent from audit lifecycle."""

    CURRENT_RUNTIME = "CURRENT_RUNTIME"
    PIT_AVAILABILITY = "PIT_AVAILABILITY"


class RuntimeDomain(StrEnum):
    """The two FRED interfaces whose health evidence must remain isolated."""

    FRED_CURRENT = "FRED_CURRENT"
    ALFRED_VINTAGE = "ALFRED_VINTAGE"


class PITAvailabilityStatus(StrEnum):
    """Fail-closed availability of one indicator and date-EOD information set."""

    AVAILABLE = "AVAILABLE"
    OUTSIDE_COVERAGE = "OUTSIDE_COVERAGE"
    NO_VINTAGE = "NO_VINTAGE"
    EMPTY_INFORMATION_SET = "EMPTY_INFORMATION_SET"
    TEMPORARY_REQUEST_FAILURE = "TEMPORARY_REQUEST_FAILURE"
    AUTHENTICATION_FAILURE = "AUTHENTICATION_FAILURE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    UNSAFE = "UNSAFE"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


class SafeErrorCategory(StrEnum):
    """Credential-free, allow-listed source failure categories."""

    NONE = "NONE"
    COVERAGE = "COVERAGE"
    EMPTY = "EMPTY"
    TRANSPORT = "TRANSPORT"
    RATE_LIMIT = "RATE_LIMIT"
    SERVER = "SERVER"
    AUTHENTICATION = "AUTHENTICATION"
    MALFORMED = "MALFORMED"
    UNSAFE = "UNSAFE"
    UNKNOWN = "UNKNOWN"


def audit_request_metadata(
    *,
    quality_scope: QualityScope,
    runtime_domain: RuntimeDomain,
    pit_status: PITAvailabilityStatus | None = None,
    runtime_blocking: bool = True,
) -> dict[str, Any]:
    """Return reserved, non-provider request audit metadata."""
    result: dict[str, Any] = {
        AUDIT_QUALITY_SCOPE: quality_scope.value,
        AUDIT_RUNTIME_DOMAIN: runtime_domain.value,
        AUDIT_RUNTIME_BLOCKING: runtime_blocking,
    }
    if pit_status is not None:
        result[AUDIT_PIT_STATUS] = pit_status.value
    return result


def pit_event_details(
    *,
    as_of: date,
    status: PITAvailabilityStatus,
    safe_error_category: SafeErrorCategory,
    http_status: int | None,
    runtime_blocking: bool,
) -> dict[str, Any]:
    """Return the typed JSON payload persisted for one PIT availability event."""
    return {
        "quality_scope": QualityScope.PIT_AVAILABILITY.value,
        "runtime_domain": RuntimeDomain.ALFRED_VINTAGE.value,
        "as_of": as_of.isoformat(),
        "pit_availability_status": status.value,
        "safe_error_category": safe_error_category.value,
        "http_status": http_status,
        "runtime_blocking": runtime_blocking,
        "classification_version": CLASSIFICATION_VERSION,
    }


def request_runtime_domain(
    request: DataSourceRequest,
    operation: str | None,
) -> RuntimeDomain | None:
    """Classify explicit or reliably linked legacy FRED request evidence."""
    explicit = request.request_parameters_json.get(AUDIT_RUNTIME_DOMAIN)
    if explicit in {item.value for item in RuntimeDomain}:
        return RuntimeDomain(explicit)
    if request.source != "fred":
        return None
    if operation == "fetch_fred_vintage":
        return RuntimeDomain.ALFRED_VINTAGE
    parameters = request.request_parameters_json
    realtime_start = parameters.get("realtime_start")
    realtime_end = parameters.get("realtime_end")
    if realtime_start is not None and realtime_start == realtime_end:
        return RuntimeDomain.ALFRED_VINTAGE
    return RuntimeDomain.FRED_CURRENT


def request_is_runtime_evidence(request: DataSourceRequest) -> bool:
    """Return whether a request can describe runtime health for its domain."""
    # Legacy rows do not carry the reserved flag and must remain conservative
    # health evidence instead of being silently discarded.
    return True


def request_blocks_runtime(request: DataSourceRequest) -> bool:
    """Return whether a failed request is evidence of a domain-level outage."""
    if request.success:
        return False
    return request.request_parameters_json.get(AUDIT_RUNTIME_BLOCKING) is not False


def quality_event_scope(session: Session, event: DataQualityEvent) -> QualityScope:
    """Classify explicit events first and legacy PIT events only from reliable evidence."""
    explicit = event.details_json.get("quality_scope")
    if explicit in {item.value for item in QualityScope}:
        return QualityScope(explicit)
    if event.run_id is None:
        return QualityScope.CURRENT_RUNTIME
    run = session.scalar(select(SyncRun).where(SyncRun.run_id == event.run_id))
    operation = run.operation if run is not None else None
    if operation == "fetch_fred_vintage":
        return QualityScope.PIT_AVAILABILITY
    if run is not None and any(key in run.parameters_json for key in ("vintage_date", "as_of")):
        return QualityScope.PIT_AVAILABILITY
    request = session.scalar(
        select(DataSourceRequest)
        .where(DataSourceRequest.sync_run_id == event.run_id)
        .order_by(DataSourceRequest.started_at.desc())
        .limit(1)
    )
    if (
        request is not None
        and request_runtime_domain(request, operation) is RuntimeDomain.ALFRED_VINTAGE
    ):
        return QualityScope.PIT_AVAILABILITY
    return QualityScope.CURRENT_RUNTIME
