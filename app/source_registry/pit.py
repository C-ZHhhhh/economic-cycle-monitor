"""On-demand arbitrary-date ALFRED snapshots for the canonical resolver."""

from datetime import date, datetime

from sqlalchemy.orm import Session

from app.data_pipeline.service import FredPipelineService
from app.data_quality.scope import PITAvailabilityStatus
from app.data_sources.fred.client import FredClient
from app.data_sources.fred.factory import create_fred_client
from app.settings import Settings
from app.source_registry.config import load_source_registry
from app.source_registry.resolver import (
    PITSnapshotNotCached,
    SourceResolutionError,
    get_effective_observations,
)
from app.source_registry.schemas import EffectiveObservation, SourceRegistryCatalog


class PITSnapshotFetchFailed(SourceResolutionError):
    """An on-demand ALFRED snapshot could not be materialized safely."""

    def __init__(self, indicator_id: str, as_of: date, status: PITAvailabilityStatus) -> None:
        super().__init__(f"PIT_{status.value}:{indicator_id}:{as_of.isoformat()}")
        self.indicator_id = indicator_id
        self.as_of = as_of
        self.availability_status = status


async def _fetch_snapshot(
    session: Session,
    settings: Settings,
    client: FredClient,
    *,
    indicator_id: str,
    as_of: date,
    registry: SourceRegistryCatalog,
) -> None:
    summary = await FredPipelineService(session, settings, client).fetch_observations_as_known_on(
        indicator_id,
        as_of,
        registry_policy_version=registry.registry_policy_version,
        registry_hash=registry.registry_hash,
    )
    if summary.status != "succeeded" or summary.failure_count:
        status = (
            summary.errors[0].pit_availability_status
            if summary.errors and summary.errors[0].pit_availability_status is not None
            else PITAvailabilityStatus.UNKNOWN_FAILURE
        )
        raise PITSnapshotFetchFailed(indicator_id, as_of, status)


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
    """Resolve canonically, fetching one exact ALFRED date only on a local PIT miss."""
    source_registry = registry or load_source_registry()
    try:
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
    except PITSnapshotNotCached:
        if not isinstance(as_of, date) or isinstance(as_of, datetime):
            raise

    runtime_settings = settings or Settings()
    if client is not None:
        await _fetch_snapshot(
            session,
            runtime_settings,
            client,
            indicator_id=indicator_id,
            as_of=as_of,
            registry=source_registry,
        )
    else:
        async with create_fred_client(runtime_settings) as runtime_client:
            await _fetch_snapshot(
                session,
                runtime_settings,
                runtime_client,
                indicator_id=indicator_id,
                as_of=as_of,
                registry=source_registry,
            )
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
