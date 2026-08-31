"""On-demand semantic ALFRED information sets for the canonical PIT resolver."""

from datetime import date, datetime
from typing import NoReturn
from uuid import UUID

from sqlalchemy.orm import Session

from app.data_pipeline.repository import (
    finish_sync_run,
    record_failed_request,
    record_successful_request,
    start_sync_run,
)
from app.data_pipeline.schemas import PipelineError, SyncSummary
from app.data_quality.checks import pit_availability_issue
from app.data_quality.repository import persist_quality_issues
from app.data_quality.scope import (
    PITAvailabilityStatus,
    QualityScope,
    RuntimeDomain,
    SafeErrorCategory,
)
from app.data_sources.fred.client import FredClient
from app.data_sources.fred.exceptions import FredRequestError, FredResponseError
from app.data_sources.fred.factory import create_fred_client
from app.database.models import SyncRun
from app.indicators.repository import get_indicator
from app.settings import Settings
from app.source_registry.config import load_source_registry
from app.source_registry.information_sets import InformationSetMalformed, persist_information_set
from app.source_registry.materialization import (
    PitMaterializationCatalog,
    load_pit_materialization_catalog,
)
from app.source_registry.pit_index import (
    ChangeIndexIncomplete,
    ChangeIndexMalformed,
    ChangeIndexNoVintage,
    ChangeIndexOutsideCoverage,
    ChangeIndexStale,
    ChangeIndexUnavailable,
    authoritative_generation,
    discover_change_index,
    latest_complete_generation,
    persist_change_index_generation,
    refresh_change_index,
)
from app.source_registry.resolver import (
    PITChangeIndexIncomplete,
    PITChangeIndexUnavailable,
    PITInformationSetNotCached,
    PITNoVintage,
    PITOutsideCoverage,
    SourceResolutionError,
    get_effective_observations,
)
from app.source_registry.schemas import EffectiveObservation, SourceRegistryCatalog

FULL_VINTAGE_COVERAGE_START = date(1776, 7, 4)


class PITSnapshotFetchFailed(SourceResolutionError):
    """An on-demand semantic ALFRED snapshot could not be materialized safely."""

    def __init__(self, indicator_id: str, as_of: date, status: PITAvailabilityStatus) -> None:
        super().__init__(f"PIT_{status.value}:{indicator_id}:{as_of.isoformat()}")
        self.indicator_id = indicator_id
        self.as_of = as_of
        self.availability_status = status


def _summary(run_id: UUID, operation: str) -> SyncSummary:
    return SyncSummary(
        run_id=run_id,
        operation=operation,
        status="succeeded",
        indicator_count=1,
    )


def _failure_status(error: Exception) -> PITAvailabilityStatus:
    if isinstance(error, ChangeIndexIncomplete):
        return PITAvailabilityStatus.CHANGE_INDEX_INCOMPLETE
    if isinstance(error, (ChangeIndexStale, ChangeIndexUnavailable)):
        return PITAvailabilityStatus.CHANGE_INDEX_UNAVAILABLE
    if isinstance(error, (ChangeIndexOutsideCoverage, PITOutsideCoverage)):
        return PITAvailabilityStatus.OUTSIDE_COVERAGE
    if isinstance(error, (ChangeIndexNoVintage, PITNoVintage)):
        return PITAvailabilityStatus.NO_VINTAGE
    if isinstance(error, (ChangeIndexMalformed, FredResponseError)):
        return PITAvailabilityStatus.MALFORMED_RESPONSE
    if isinstance(error, InformationSetMalformed) and str(error) == "PIT_EMPTY_INFORMATION_SET":
        return PITAvailabilityStatus.EMPTY_INFORMATION_SET
    if isinstance(error, InformationSetMalformed):
        return PITAvailabilityStatus.INFORMATION_SET_MALFORMED
    if isinstance(error, FredRequestError) and error.status_code in {401, 403}:
        return PITAvailabilityStatus.AUTHENTICATION_FAILURE
    if isinstance(error, FredRequestError) and (
        error.status_code == 429 or (error.status_code is not None and error.status_code >= 500)
    ):
        return PITAvailabilityStatus.TEMPORARY_REQUEST_FAILURE
    if isinstance(error, FredRequestError) and error.status_code == 400:
        return PITAvailabilityStatus.UNKNOWN_FAILURE
    if isinstance(error, FredRequestError):
        return PITAvailabilityStatus.TEMPORARY_REQUEST_FAILURE
    return PITAvailabilityStatus.UNKNOWN_FAILURE


def _safe_error_category(status: PITAvailabilityStatus) -> SafeErrorCategory:
    if status in {
        PITAvailabilityStatus.OUTSIDE_COVERAGE,
        PITAvailabilityStatus.NO_VINTAGE,
    }:
        return SafeErrorCategory.COVERAGE
    if status is PITAvailabilityStatus.EMPTY_INFORMATION_SET:
        return SafeErrorCategory.EMPTY
    if status is PITAvailabilityStatus.AUTHENTICATION_FAILURE:
        return SafeErrorCategory.AUTHENTICATION
    if status in {
        PITAvailabilityStatus.MALFORMED_RESPONSE,
        PITAvailabilityStatus.INFORMATION_SET_MALFORMED,
    }:
        return SafeErrorCategory.MALFORMED
    if status in {
        PITAvailabilityStatus.CHANGE_INDEX_INCOMPLETE,
        PITAvailabilityStatus.CHANGE_INDEX_UNAVAILABLE,
    }:
        return SafeErrorCategory.INDEX
    if status is PITAvailabilityStatus.INFORMATION_SET_HASH_MISMATCH:
        return SafeErrorCategory.INTEGRITY
    if status is PITAvailabilityStatus.UNSAFE:
        return SafeErrorCategory.UNSAFE
    if status is PITAvailabilityStatus.TEMPORARY_REQUEST_FAILURE:
        return SafeErrorCategory.TRANSPORT
    return SafeErrorCategory.UNKNOWN


def _fail_run(
    session: Session,
    *,
    run_id: UUID,
    run: SyncRun,
    summary: SyncSummary,
    indicator_id: str,
    series_id: str,
    endpoint: str,
    parameters: dict[str, object],
    error: Exception,
    as_of: date,
) -> NoReturn:
    status = _failure_status(error)
    runtime_blocking = status not in {
        PITAvailabilityStatus.OUTSIDE_COVERAGE,
        PITAvailabilityStatus.NO_VINTAGE,
        PITAvailabilityStatus.EMPTY_INFORMATION_SET,
        PITAvailabilityStatus.UNSAFE,
    }
    summary.status = "failed"
    summary.failure_count = 1
    summary.request_count += 1
    summary.errors.append(
        PipelineError(
            indicator_id=indicator_id,
            error_type=type(error).__name__,
            message=f"PIT_{status.value}",
            quality_scope=QualityScope.PIT_AVAILABILITY,
            runtime_domain=RuntimeDomain.ALFRED_VINTAGE,
            pit_availability_status=status,
        )
    )
    record_failed_request(
        session,
        run_id,
        endpoint=endpoint,
        series_id=series_id,
        parameters=parameters,
        error=error,
        quality_scope=QualityScope.PIT_AVAILABILITY,
        runtime_domain=RuntimeDomain.ALFRED_VINTAGE,
        pit_status=status,
        runtime_blocking=runtime_blocking,
    )
    http_status = error.status_code if isinstance(error, FredRequestError) else None
    summary.quality_event_count += persist_quality_issues(
        session,
        [
            pit_availability_issue(
                indicator_id,
                as_of=as_of,
                status=status,
                safe_error_category=_safe_error_category(status),
                http_status=http_status,
                runtime_blocking=runtime_blocking,
            )
        ],
        run_id=run_id,
    )
    finish_sync_run(session, run, summary)
    raise PITSnapshotFetchFailed(indicator_id, as_of, status) from error


async def _discover_and_persist_index(
    session: Session,
    client: FredClient,
    *,
    indicator_id: str,
    series_id: str,
    evaluation_as_of: date,
    registry: SourceRegistryCatalog,
    materialization: PitMaterializationCatalog,
) -> None:
    operation = "discover_pit_change_index"
    prior = latest_complete_generation(
        session,
        indicator_id=indicator_id,
        provider_series_id=series_id,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
    )
    is_incremental = (
        prior is not None
        and prior.index_complete_through is not None
        and evaluation_as_of > prior.index_complete_through
    )
    coverage_start = (
        prior.index_complete_through.fromordinal(prior.index_complete_through.toordinal() + 1)
        if is_incremental and prior is not None and prior.index_complete_through is not None
        else FULL_VINTAGE_COVERAGE_START
    )
    run = start_sync_run(
        session,
        operation,
        {
            "indicator_id": indicator_id,
            "coverage_start": coverage_start.isoformat(),
            "coverage_end": evaluation_as_of.isoformat(),
            "refresh_mode": "INCREMENTAL" if is_incremental else "FULL_DISCOVERY",
            "prior_generation_hash": prior.generation_hash if is_incremental and prior else None,
            "quality_scope": QualityScope.PIT_AVAILABILITY.value,
            "runtime_domain": RuntimeDomain.ALFRED_VINTAGE.value,
        },
        1,
    )
    summary = _summary(run.run_id, operation)
    try:
        if is_incremental and prior is not None:
            discovery, payloads = await refresh_change_index(
                client,
                prior=prior,
                coverage_end=evaluation_as_of,
                source_registry_hash=registry.registry_hash,
                materialization=materialization,
            )
        else:
            discovery, payloads = await discover_change_index(
                client,
                indicator_id=indicator_id,
                provider_series_id=series_id,
                coverage_start=FULL_VINTAGE_COVERAGE_START,
                coverage_end=evaluation_as_of,
                source_registry_hash=registry.registry_hash,
                materialization=materialization,
            )
    except Exception as error:
        _fail_run(
            session,
            run_id=run.run_id,
            run=run,
            summary=summary,
            indicator_id=indicator_id,
            series_id=series_id,
            endpoint="series/vintagedates",
            parameters={
                "realtime_start": coverage_start,
                "realtime_end": evaluation_as_of,
            },
            error=error,
            as_of=evaluation_as_of,
        )
    for payload in payloads:
        record_successful_request(
            session,
            run.run_id,
            payload,
            quality_scope=QualityScope.PIT_AVAILABILITY,
            runtime_domain=RuntimeDomain.ALFRED_VINTAGE,
            pit_status=PITAvailabilityStatus.AVAILABLE,
            runtime_blocking=False,
        )
    persist_change_index_generation(
        session,
        indicator_id=indicator_id,
        provider_series_id=series_id,
        discovery=discovery,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
        discovered_at=payloads[-1].finished_at,
        sync_run_id=run.run_id,
    )
    summary.success_count = 1
    summary.request_count = len(payloads)
    summary.cache_hit_count = sum(int(item.from_cache) for item in payloads)
    finish_sync_run(session, run, summary)


async def _materialize_information_set(
    session: Session,
    client: FredClient,
    *,
    indicator_id: str,
    source_date: date,
    evaluation_as_of: date,
    registry: SourceRegistryCatalog,
    materialization: PitMaterializationCatalog,
) -> None:
    indicator = get_indicator(session, indicator_id)
    if indicator is None or indicator.fred_series_id is None:
        raise SourceResolutionError(f"INDICATOR_NOT_MATERIALIZED:{indicator_id}")
    policy = registry.by_indicator()[indicator_id]
    generation = authoritative_generation(
        session,
        indicator_id=indicator_id,
        provider_series_id=indicator.fred_series_id,
        evaluation_as_of=evaluation_as_of,
        source_registry_hash=registry.registry_hash,
        materialization=materialization,
    )
    operation = "materialize_pit_information_set"
    run = start_sync_run(
        session,
        operation,
        {
            "indicator_id": indicator_id,
            "evaluation_as_of": evaluation_as_of.isoformat(),
            "source_information_set_date": source_date.isoformat(),
            "quality_scope": QualityScope.PIT_AVAILABILITY.value,
            "runtime_domain": RuntimeDomain.ALFRED_VINTAGE.value,
        },
        1,
    )
    summary = _summary(run.run_id, operation)
    try:
        observations, payload = await client.get_observations(
            indicator.fred_series_id,
            realtime_start=source_date,
            realtime_end=source_date,
        )
        record_successful_request(
            session,
            run.run_id,
            payload,
            quality_scope=QualityScope.PIT_AVAILABILITY,
            runtime_domain=RuntimeDomain.ALFRED_VINTAGE,
            pit_status=PITAvailabilityStatus.AVAILABLE,
            runtime_blocking=False,
        )
        information_set = persist_information_set(
            session,
            indicator_id=indicator_id,
            provider_series_id=indicator.fred_series_id,
            source_information_set_date=source_date,
            unit=policy.canonical.unit,
            frequency=policy.canonical.frequency,
            observations=observations,
            raw_payload_hash=payload.response_hash,
            retrieved_at=payload.finished_at,
            generation=generation,
            source_registry_hash=registry.registry_hash,
            materialization=materialization,
            sync_run_id=run.run_id,
        )
    except Exception as error:
        _fail_run(
            session,
            run_id=run.run_id,
            run=run,
            summary=summary,
            indicator_id=indicator_id,
            series_id=indicator.fred_series_id,
            endpoint="series/observations",
            parameters={"realtime_start": source_date, "realtime_end": source_date},
            error=error,
            as_of=evaluation_as_of,
        )
    summary.success_count = 1
    summary.request_count = 1
    summary.cache_hit_count = int(payload.from_cache)
    summary.inserted_count = information_set.observation_count
    finish_sync_run(session, run, summary)


async def _resolve_with_client(
    session: Session,
    client: FredClient,
    *,
    indicator_id: str,
    start: date,
    end: date,
    as_of: date,
    registry: SourceRegistryCatalog,
    materialization: PitMaterializationCatalog,
    today: date | None,
    evaluated_at: datetime | None,
) -> list[EffectiveObservation]:
    try:
        return get_effective_observations(
            session,
            indicator_id,
            start,
            end,
            as_of,
            registry=registry,
            materialization=materialization,
            today=today,
            evaluated_at=evaluated_at,
        )
    except (PITChangeIndexIncomplete, PITChangeIndexUnavailable):
        indicator = get_indicator(session, indicator_id)
        if indicator is None or indicator.fred_series_id is None:
            raise
        await _discover_and_persist_index(
            session,
            client,
            indicator_id=indicator_id,
            series_id=indicator.fred_series_id,
            evaluation_as_of=as_of,
            registry=registry,
            materialization=materialization,
        )
    except (PITOutsideCoverage, PITNoVintage):
        raise
    try:
        return get_effective_observations(
            session,
            indicator_id,
            start,
            end,
            as_of,
            registry=registry,
            materialization=materialization,
            today=today,
            evaluated_at=evaluated_at,
        )
    except PITInformationSetNotCached as error:
        await _materialize_information_set(
            session,
            client,
            indicator_id=indicator_id,
            source_date=error.source_information_set_date,
            evaluation_as_of=as_of,
            registry=registry,
            materialization=materialization,
        )
    return get_effective_observations(
        session,
        indicator_id,
        start,
        end,
        as_of,
        registry=registry,
        materialization=materialization,
        today=today,
        evaluated_at=evaluated_at,
    )


async def get_effective_observations_on_demand(
    session: Session,
    indicator_id: str,
    start: date,
    end: date,
    as_of: date | datetime | None = None,
    *,
    registry: SourceRegistryCatalog | None = None,
    settings: Settings | None = None,
    client: FredClient | None = None,
    today: date | None = None,
    evaluated_at: datetime | None = None,
) -> list[EffectiveObservation]:
    """Resolve current data unchanged or materialize one semantic PIT information set."""
    source_registry = registry or load_source_registry()
    if as_of is None or isinstance(as_of, datetime):
        return get_effective_observations(
            session,
            indicator_id,
            start,
            end,
            as_of,
            registry=source_registry,
            today=today,
            evaluated_at=evaluated_at,
        )
    materialization = load_pit_materialization_catalog(source_registry=source_registry)
    if client is not None:
        return await _resolve_with_client(
            session,
            client,
            indicator_id=indicator_id,
            start=start,
            end=end,
            as_of=as_of,
            registry=source_registry,
            materialization=materialization,
            today=today,
            evaluated_at=evaluated_at,
        )
    runtime_settings = settings or Settings()
    async with create_fred_client(runtime_settings) as runtime_client:
        return await _resolve_with_client(
            session,
            runtime_client,
            indicator_id=indicator_id,
            start=start,
            end=end,
            as_of=as_of,
            registry=source_registry,
            materialization=materialization,
            today=today,
            evaluated_at=evaluated_at,
        )
