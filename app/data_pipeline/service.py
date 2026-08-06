"""Stage 2 FRED metadata, backfill, incremental, and vintage workflows."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar

from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from app.data_pipeline.repository import (
    finish_sync_run,
    latest_observation_date,
    persist_as_known_observations,
    persist_observations,
    record_failed_request,
    record_successful_request,
    start_sync_run,
)
from app.data_pipeline.schemas import (
    BackfillOptions,
    PipelineError,
    PipelineSelection,
    SyncOptions,
    SyncSummary,
)
from app.data_quality.checks import (
    check_metadata_validation,
    check_observation_batch,
    source_unavailable_issue,
)
from app.data_quality.repository import persist_quality_issues, reconcile_quality_issues
from app.data_sources.fred.client import FredClient
from app.data_sources.fred.metadata import validate_series_metadata
from app.data_sources.fred.schemas import (
    FredObservation,
    FredPayload,
    FredRelease,
    FredSeriesMetadata,
)
from app.database.models import IndicatorRegistry
from app.indicators.config import load_indicator_catalog
from app.indicators.repository import get_indicator, list_indicators
from app.settings import Settings

T = TypeVar("T")


class FredPipelineService:
    """Coordinate source calls and persistence while isolating indicator failures."""

    def __init__(self, session: Session, settings: Settings, client: FredClient) -> None:
        self.session = session
        self.settings = settings
        self.client = client
        self._definitions = {
            item.indicator_id: item for item in load_indicator_catalog().indicators
        }

    def _select(self, selection: PipelineSelection) -> Sequence[IndicatorRegistry]:
        return (
            list_indicators(
                self.session,
                module=selection.module,
                enabled=True if selection.enabled_only else None,
                offset=0,
                limit=100,
            )
            if selection.indicator_id is None
            else self._select_one(selection)
        )

    def _select_one(self, selection: PipelineSelection) -> Sequence[IndicatorRegistry]:
        assert selection.indicator_id is not None
        item = get_indicator(self.session, selection.indicator_id)
        if item is None:
            return []
        if selection.enabled_only and not item.enabled:
            return []
        if selection.module is not None and item.module != selection.module:
            return []
        return [item]

    async def _bounded(
        self,
        items: Sequence[IndicatorRegistry],
        operation: Callable[[IndicatorRegistry], Awaitable[T]],
    ) -> list[T | BaseException]:
        limit = min(self.settings.fetch_concurrency, self.settings.per_source_fetch_concurrency)
        semaphore = asyncio.Semaphore(limit)

        async def invoke(item: IndicatorRegistry) -> T:
            async with semaphore:
                return await operation(item)

        return await asyncio.gather(*(invoke(item) for item in items), return_exceptions=True)

    @staticmethod
    def _finish_status(summary: SyncSummary) -> None:
        if summary.failure_count == 0:
            summary.status = "succeeded"
        elif summary.success_count or summary.skipped_count:
            summary.status = "partial"
        else:
            summary.status = "failed"

    async def validate_metadata(self, selection: PipelineSelection) -> SyncSummary:
        """Fetch, store, and semantically validate source metadata."""
        indicators = self._select(selection)
        run = start_sync_run(
            self.session,
            "validate_fred_metadata",
            selection.model_dump(mode="json"),
            len(indicators),
        )
        summary = SyncSummary(
            run_id=run.run_id,
            operation=run.operation,
            status="succeeded",
            indicator_count=len(indicators),
        )

        async def fetch(
            item: IndicatorRegistry,
        ) -> tuple[FredSeriesMetadata, FredPayload, FredRelease, FredPayload]:
            assert item.fred_series_id is not None
            metadata, metadata_payload = await self.client.get_series_metadata(item.fred_series_id)
            release, release_payload = await self.client.get_release(item.fred_series_id)
            return metadata, metadata_payload, release, release_payload

        outcomes = await self._bounded(indicators, fetch)
        for indicator, outcome in zip(indicators, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                error = outcome if isinstance(outcome, Exception) else RuntimeError(str(outcome))
                self._record_failure(summary, run.run_id, indicator, "series", {}, error)
                indicator.metadata_status = "unavailable"
                indicator.metadata_checked_at = datetime.now(UTC)
                continue
            metadata, metadata_payload, release, release_payload = outcome
            record_successful_request(self.session, run.run_id, metadata_payload)
            record_successful_request(self.session, run.run_id, release_payload)
            summary.request_count += 2
            summary.cache_hit_count += int(metadata_payload.from_cache) + int(
                release_payload.from_cache
            )
            definition = self._definitions[indicator.indicator_id]
            validation = validate_series_metadata(definition, metadata)
            metadata_issues = check_metadata_validation(indicator.indicator_id, validation)
            inserted, _resolved = reconcile_quality_issues(
                self.session,
                metadata_issues,
                run_id=run.run_id,
                indicator_id=indicator.indicator_id,
                event_types=(
                    "metadata_frequency_mismatch",
                    "metadata_unit_mismatch",
                    "metadata_seasonal_adjustment_mismatch",
                ),
            )
            summary.quality_event_count += inserted
            indicator.metadata_status = validation.status
            indicator.metadata_checked_at = datetime.now(UTC)
            indicator.source_title = metadata.title
            indicator.source_notes = metadata.notes
            indicator.source_frequency = metadata.frequency
            indicator.source_units = metadata.units
            indicator.source_seasonal_adjustment = metadata.seasonal_adjustment
            indicator.source_observation_start = metadata.observation_start
            indicator.source_observation_end = metadata.observation_end
            indicator.source_last_updated = metadata.last_updated
            indicator.source_popularity = metadata.popularity
            indicator.source_release_json = release.model_dump(mode="json")
            indicator.metadata_warnings_json = [*validation.errors, *validation.warnings]
            summary.success_count += 1
            summary.results.append(
                {
                    "indicator_id": indicator.indicator_id,
                    "series_id": definition.fred_series_id,
                    "status": validation.status,
                    "errors": validation.errors,
                    "warnings": validation.warnings,
                    "metadata": metadata.model_dump(mode="json"),
                    "release": release.model_dump(mode="json"),
                }
            )
            if validation.status == "invalid":
                summary.failure_count += 1
                summary.success_count -= 1
        self._finish_status(summary)
        finish_sync_run(self.session, run, summary)
        return summary

    async def backfill(self, options: BackfillOptions) -> SyncSummary:
        """Fetch bounded history for validated indicators and persist idempotently."""
        selected = self._select(options)
        indicators = [
            item for item in selected if item.metadata_status in {"valid", "valid_with_warning"}
        ]
        return await self._fetch_current(
            "backfill_fred",
            indicators,
            options,
            options.start,
            pre_skipped=len(selected) - len(indicators),
        )

    async def sync(self, options: SyncOptions) -> SyncSummary:
        """Fetch recent windows and preserve revisions to existing dates."""
        selected = self._select(options)
        indicators = [
            item for item in selected if item.metadata_status in {"valid", "valid_with_warning"}
        ]
        return await self._fetch_current(
            "sync_fred",
            indicators,
            options,
            None,
            pre_skipped=len(selected) - len(indicators),
        )

    async def _fetch_current(
        self,
        operation: str,
        indicators: Sequence[IndicatorRegistry],
        options: BackfillOptions | SyncOptions,
        fixed_start: date | None,
        *,
        pre_skipped: int,
    ) -> SyncSummary:
        run = start_sync_run(
            self.session,
            operation,
            options.model_dump(mode="json"),
            len(indicators) + pre_skipped,
        )
        summary = SyncSummary(
            run_id=run.run_id,
            operation=operation,
            status="succeeded",
            indicator_count=len(indicators) + pre_skipped,
            skipped_count=pre_skipped,
        )

        async def fetch(item: IndicatorRegistry) -> tuple[list[FredObservation], FredPayload, date]:
            assert item.fred_series_id is not None
            if fixed_start is not None:
                start = fixed_start
            else:
                assert isinstance(options, SyncOptions)
                start = self._sync_start(item, options)
            if item.source_observation_start is not None:
                start = max(start, item.source_observation_start)
            end = options.end if isinstance(options, BackfillOptions) else date.today()
            observations, payload = await self.client.get_observations(
                item.fred_series_id, observation_start=start, observation_end=end
            )
            return observations, payload, start

        outcomes = await self._bounded(indicators, fetch)
        for indicator, outcome in zip(indicators, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                error = outcome if isinstance(outcome, Exception) else RuntimeError(str(outcome))
                self._record_failure(
                    summary, run.run_id, indicator, "series/observations", {}, error
                )
                continue
            observations, payload, start = outcome
            record_successful_request(self.session, run.run_id, payload)
            summary.request_count += 1
            summary.cache_hit_count += int(payload.from_cache)
            issues = check_observation_batch(indicator.indicator_id, observations)
            summary.quality_event_count += persist_quality_issues(
                self.session, issues, run_id=run.run_id
            )
            if not observations:
                summary.skipped_count += 1
                summary.results.append(
                    {"indicator_id": indicator.indicator_id, "status": "empty", "start": start}
                )
                continue
            write = persist_observations(
                self.session,
                indicator=indicator,
                observations=observations,
                payload_hash=payload.response_hash,
                retrieved_at=payload.finished_at,
                dry_run=options.dry_run,
                batch_size=self.settings.database_batch_size,
            )
            summary.success_count += 1
            summary.inserted_count += write.inserted
            summary.revised_count += write.revised
            summary.results.append(
                {"indicator_id": indicator.indicator_id, **write.model_dump(), "start": start}
            )
        self._finish_status(summary)
        finish_sync_run(self.session, run, summary)
        return summary

    def _sync_start(self, item: IndicatorRegistry, options: SyncOptions) -> date:
        if options.since is not None:
            return options.since
        latest = (
            latest_observation_date(self.session, item.indicator_id)
            or self.settings.fred_default_backfill_start
        )
        if options.lookback_days is not None:
            return latest - timedelta(days=options.lookback_days)
        if item.observation_frequency == "weekly":
            return latest - timedelta(days=self.settings.fred_weekly_lookback_days)
        if item.observation_frequency == "quarterly":
            return latest - relativedelta(months=3 * self.settings.fred_quarterly_lookback_quarters)
        return latest - relativedelta(months=self.settings.fred_monthly_lookback_months)

    async def fetch_observations_as_known_on(
        self, indicator_id: str, vintage_date: date
    ) -> SyncSummary:
        """Fetch and retain one explicit ALFRED snapshot without bulk vintage crawling."""
        indicator = get_indicator(self.session, indicator_id)
        indicators = [indicator] if indicator is not None else []
        run = start_sync_run(
            self.session,
            "fetch_fred_vintage",
            {"indicator_id": indicator_id, "vintage_date": vintage_date.isoformat()},
            len(indicators),
        )
        summary = SyncSummary(
            run_id=run.run_id,
            operation=run.operation,
            status="succeeded",
            indicator_count=len(indicators),
        )
        if indicator is None or indicator.fred_series_id is None:
            summary.failure_count = 1
            summary.errors.append(
                PipelineError(
                    indicator_id=indicator_id,
                    error_type="IndicatorNotFound",
                    message="Indicator is not registered with a FRED Series ID",
                )
            )
        else:
            try:
                observations, payload = await self.client.get_observations(
                    indicator.fred_series_id,
                    realtime_start=vintage_date,
                    realtime_end=vintage_date,
                )
                record_successful_request(self.session, run.run_id, payload)
                summary.request_count = 1
                summary.cache_hit_count = int(payload.from_cache)
                write = persist_as_known_observations(
                    self.session,
                    indicator=indicator,
                    observations=observations,
                    vintage_date=vintage_date,
                    payload_hash=payload.response_hash,
                    retrieved_at=payload.finished_at,
                )
                summary.success_count = 1
                summary.inserted_count = write.inserted
                summary.quality_event_count = write.missing
                summary.results.append(
                    {
                        "indicator_id": indicator_id,
                        "vintage_date": vintage_date,
                        **write.model_dump(),
                    }
                )
            except Exception as error:
                self._record_failure(
                    summary,
                    run.run_id,
                    indicator,
                    "series/observations",
                    {"realtime_start": vintage_date, "realtime_end": vintage_date},
                    error,
                )
        self._finish_status(summary)
        finish_sync_run(self.session, run, summary)
        return summary

    def _record_failure(
        self,
        summary: SyncSummary,
        run_id: Any,
        indicator: IndicatorRegistry,
        endpoint: str,
        parameters: dict[str, Any],
        error: Exception,
    ) -> None:
        summary.failure_count += 1
        summary.request_count += 1
        summary.errors.append(
            PipelineError(
                indicator_id=indicator.indicator_id,
                error_type=type(error).__name__,
                message=str(error),
            )
        )
        record_failed_request(
            self.session,
            run_id,
            endpoint=endpoint,
            series_id=indicator.fred_series_id or "",
            parameters=parameters,
            error=error,
        )
        summary.quality_event_count += persist_quality_issues(
            self.session,
            [source_unavailable_issue(indicator.indicator_id, error)],
            run_id=run_id,
        )
