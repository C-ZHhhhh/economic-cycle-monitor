"""BLS mapping validation, batched history, and incremental synchronization."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.data_pipeline.repository import (
    finish_sync_run,
    latest_source_observation_date,
    persist_official_observations,
    record_failed_bls_request,
    record_successful_bls_request,
    start_sync_run,
)
from app.data_pipeline.schemas import (
    BlsBackfillOptions,
    BlsSyncOptions,
    PipelineError,
    PipelineSelection,
    SyncSummary,
)
from app.data_quality.checks import (
    check_official_observation_batch,
    official_source_issue,
)
from app.data_quality.repository import persist_quality_issues
from app.data_sources.bls.adapter import BlsAdapter
from app.data_sources.bls.client import BlsClient, split_year_ranges
from app.data_sources.bls.exceptions import BlsBusinessError
from app.data_sources.bls.metadata import validate_bls_metadata
from app.data_sources.bls.schemas import BlsSeries
from app.database.models import IndicatorRegistry, IndicatorSourceMapping
from app.indicators.repository import get_indicator
from app.settings import Settings
from app.source_mappings.config import load_source_mapping_catalog
from app.source_mappings.repository import list_source_mappings, seed_source_mappings
from app.source_mappings.schemas import SourceMappingDefinition


class BlsPipelineService:
    """Coordinate BLS batches while isolating series-level validation and writes."""

    def __init__(self, session: Session, settings: Settings, client: BlsClient) -> None:
        self.session = session
        self.settings = settings
        self.client = client
        self.catalog = load_source_mapping_catalog()
        self.definitions = {item.indicator_id: item for item in self.catalog.mappings}

    def _select(
        self, selection: PipelineSelection, *, usable_only: bool = False
    ) -> list[tuple[IndicatorRegistry, IndicatorSourceMapping, SourceMappingDefinition]]:
        seed_source_mappings(self.session, self.catalog)
        mappings = list_source_mappings(
            self.session,
            source="bls",
            indicator_id=selection.indicator_id,
            enabled=True if selection.enabled_only else None,
            limit=100,
        )
        selected: list[
            tuple[IndicatorRegistry, IndicatorSourceMapping, SourceMappingDefinition]
        ] = []
        for mapping in mappings:
            definition = self.definitions.get(mapping.indicator_id)
            indicator = get_indicator(self.session, mapping.indicator_id)
            if definition is None or indicator is None:
                continue
            if selection.module is not None and indicator.module != selection.module:
                continue
            if selection.enabled_only and not indicator.enabled:
                continue
            if usable_only and mapping.mapping_status not in {"valid", "valid_with_warning"}:
                continue
            selected.append((indicator, mapping, definition))
        return selected

    @staticmethod
    def _finish_status(summary: SyncSummary) -> None:
        if summary.failure_count == 0:
            summary.status = "succeeded"
        elif summary.success_count or summary.skipped_count:
            summary.status = "partial"
        else:
            summary.status = "failed"

    async def validate_mappings(self, selection: PipelineSelection) -> SyncSummary:
        """Validate exact IDs and optional BLS catalog fields in one batch."""
        selected = self._select(selection)
        run = start_sync_run(
            self.session,
            "validate_bls_mappings",
            selection.model_dump(mode="json"),
            len(selected),
        )
        summary = SyncSummary(
            run_id=run.run_id,
            operation=run.operation,
            status="succeeded",
            indicator_count=len(selected),
        )
        if not selected:
            self._finish_status(summary)
            finish_sync_run(self.session, run, summary)
            return summary
        ids = [mapping.source_series_id for _, mapping, _ in selected]
        current_year = date.today().year
        try:
            series, payload = await self.client.get_series_data(
                ids, start_year=current_year, end_year=current_year, catalog=True
            )
            record_successful_bls_request(self.session, run.run_id, payload)
            summary.request_count = 1
            summary.cache_hit_count = int(payload.from_cache)
        except Exception as error:
            record_failed_bls_request(
                self.session,
                run.run_id,
                series_ids=ids,
                start_year=current_year,
                end_year=current_year,
                error=error,
            )
            summary.request_count = 1
            for indicator, mapping, _ in selected:
                mapping.mapping_status = "unavailable"
                mapping.metadata_checked_at = datetime.now(UTC)
                self._add_failure(summary, indicator.indicator_id, error)
                summary.quality_event_count += persist_quality_issues(
                    self.session,
                    [
                        official_source_issue(
                            indicator.indicator_id,
                            event_type=(
                                "source_business_error"
                                if isinstance(error, BlsBusinessError)
                                else "source_series_unavailable"
                            ),
                            severity="warning",
                            source="bls",
                            message=f"BLS mapping validation failed: {type(error).__name__}",
                        )
                    ],
                    run_id=run.run_id,
                )
            self._finish_status(summary)
            finish_sync_run(self.session, run, summary)
            return summary
        returned = {item.seriesID: item for item in series}
        for indicator, mapping, definition in selected:
            source_series = returned.get(mapping.source_series_id)
            if source_series is None:
                mapping.mapping_status = "unavailable"
                mapping.mapping_warnings_json = ["BLS response omitted the requested Series ID"]
                mapping.metadata_checked_at = datetime.now(UTC)
                missing_error = RuntimeError("BLS response omitted the requested Series ID")
                self._add_failure(summary, indicator.indicator_id, missing_error)
                summary.quality_event_count += persist_quality_issues(
                    self.session,
                    [
                        official_source_issue(
                            indicator.indicator_id,
                            event_type="source_response_partial",
                            severity="warning",
                            source="bls",
                            message="BLS batch response omitted a requested Series ID",
                            details={"source_series_id": mapping.source_series_id},
                        )
                    ],
                    run_id=run.run_id,
                )
                continue
            validation = validate_bls_metadata(definition, source_series)
            mapping.mapping_status = validation.status
            mapping.mapping_warnings_json = [*validation.errors, *validation.warnings]
            mapping.metadata_checked_at = datetime.now(UTC)
            summary.results.append(
                {
                    "indicator_id": indicator.indicator_id,
                    "source_series_id": mapping.source_series_id,
                    "status": validation.status,
                    "errors": validation.errors,
                    "warnings": validation.warnings,
                }
            )
            if validation.errors:
                summary.failure_count += 1
                summary.quality_event_count += persist_quality_issues(
                    self.session,
                    [
                        official_source_issue(
                            indicator.indicator_id,
                            event_type="source_metadata_mismatch",
                            severity="error",
                            source="bls",
                            message=message,
                        )
                        for message in validation.errors
                    ],
                    run_id=run.run_id,
                )
            else:
                summary.success_count += 1
        self._finish_status(summary)
        finish_sync_run(self.session, run, summary)
        return summary

    async def backfill(self, options: BlsBackfillOptions) -> SyncSummary:
        """Fetch each configured year window as one multi-series BLS request."""
        if options.start_year > options.end_year:
            raise ValueError("start_year must not be after end_year")
        selected = self._select(options, usable_only=True)
        windows = split_year_ranges(
            options.start_year, options.end_year, self.client.max_years_per_request
        )
        return await self._fetch_windows("backfill_bls", selected, windows, options.dry_run)

    async def sync(self, options: BlsSyncOptions) -> SyncSummary:
        """Batch mappings by their source-aware incremental lookback start year."""
        selected = self._select(options, usable_only=True)
        current_year = date.today().year
        groups: dict[
            int, list[tuple[IndicatorRegistry, IndicatorSourceMapping, SourceMappingDefinition]]
        ] = defaultdict(list)
        for item in selected:
            indicator, mapping, _ = item
            latest = latest_source_observation_date(
                self.session, indicator.indicator_id, source="bls"
            )
            start_year = (
                max(
                    self.settings.bls_default_backfill_start_year,
                    latest.year - mapping.revision_lookback_years,
                )
                if latest is not None
                else self.settings.bls_default_backfill_start_year
            )
            groups[start_year].append(item)
        return await self._fetch_groups(
            "sync_bls",
            groups,
            end_year=current_year,
            dry_run=options.dry_run,
            indicator_count=len(selected),
        )

    async def _fetch_windows(
        self,
        operation: str,
        selected: Sequence[
            tuple[IndicatorRegistry, IndicatorSourceMapping, SourceMappingDefinition]
        ],
        windows: list[tuple[int, int]],
        dry_run: bool,
    ) -> SyncSummary:
        groups = {start: list(selected) for start, _ in windows}
        end_by_start = {start: end for start, end in windows}
        return await self._fetch_groups(
            operation,
            groups,
            end_year=None,
            dry_run=dry_run,
            indicator_count=len(selected),
            end_by_start=end_by_start,
        )

    async def _fetch_groups(
        self,
        operation: str,
        groups: dict[
            int, list[tuple[IndicatorRegistry, IndicatorSourceMapping, SourceMappingDefinition]]
        ],
        *,
        end_year: int | None,
        dry_run: bool,
        indicator_count: int,
        end_by_start: dict[int, int] | None = None,
    ) -> SyncSummary:
        parameters: dict[str, Any] = {
            "start_years": sorted(groups),
            "end_year": end_year,
            "dry_run": dry_run,
        }
        run = start_sync_run(self.session, operation, parameters, indicator_count)
        summary = SyncSummary(
            run_id=run.run_id,
            operation=operation,
            status="succeeded",
            indicator_count=indicator_count,
        )
        counters: dict[str, dict[str, int]] = defaultdict(
            lambda: {"inserted": 0, "revised": 0, "unchanged": 0, "missing": 0}
        )
        successes: set[str] = set()
        failures: set[str] = set()
        for start_year, group in sorted(groups.items()):
            group_end = end_by_start[start_year] if end_by_start else end_year
            assert group_end is not None
            ids = [mapping.source_series_id for _, mapping, _ in group]
            try:
                series, payload = await self.client.get_series_data(
                    ids, start_year=start_year, end_year=group_end
                )
                record_successful_bls_request(self.session, run.run_id, payload)
                summary.request_count += 1
                summary.cache_hit_count += int(payload.from_cache)
            except Exception as error:
                record_failed_bls_request(
                    self.session,
                    run.run_id,
                    series_ids=ids,
                    start_year=start_year,
                    end_year=group_end,
                    error=error,
                )
                summary.request_count += 1
                for indicator, _, _ in group:
                    failures.add(indicator.indicator_id)
                    self._add_failure(summary, indicator.indicator_id, error, count=False)
                    summary.quality_event_count += persist_quality_issues(
                        self.session,
                        [
                            official_source_issue(
                                indicator.indicator_id,
                                event_type=(
                                    "source_business_error"
                                    if isinstance(error, BlsBusinessError)
                                    else "source_series_unavailable"
                                ),
                                severity="warning",
                                source="bls",
                                message=f"BLS request failed: {type(error).__name__}",
                                details={"start_year": start_year, "end_year": group_end},
                            )
                        ],
                        run_id=run.run_id,
                    )
                continue
            returned: dict[str, BlsSeries] = {item.seriesID: item for item in series}
            for indicator, mapping, _ in group:
                source_series = returned.get(mapping.source_series_id)
                if source_series is None:
                    failures.add(indicator.indicator_id)
                    missing_error = RuntimeError("BLS response omitted the requested Series ID")
                    self._add_failure(summary, indicator.indicator_id, missing_error, count=False)
                    summary.quality_event_count += persist_quality_issues(
                        self.session,
                        [
                            official_source_issue(
                                indicator.indicator_id,
                                event_type="source_response_partial",
                                severity="warning",
                                source="bls",
                                message="BLS batch response omitted a requested Series ID",
                                details={"source_series_id": mapping.source_series_id},
                            )
                        ],
                        run_id=run.run_id,
                    )
                    continue
                observations = BlsAdapter.observations(source_series)
                issues = check_official_observation_batch(
                    indicator.indicator_id, observations, source="bls"
                )
                summary.quality_event_count += persist_quality_issues(
                    self.session, issues, run_id=run.run_id
                )
                if not observations:
                    failures.add(indicator.indicator_id)
                    continue
                write = persist_official_observations(
                    self.session,
                    indicator=indicator,
                    source="bls",
                    source_series_id=mapping.source_series_id,
                    observations=observations,
                    payload_hash=payload.response_hash,
                    retrieved_at=payload.finished_at,
                    dry_run=dry_run,
                    batch_size=self.settings.database_batch_size,
                )
                successes.add(indicator.indicator_id)
                if write.revised:
                    summary.quality_event_count += persist_quality_issues(
                        self.session,
                        [
                            official_source_issue(
                                indicator.indicator_id,
                                event_type="source_revision_detected",
                                severity="info",
                                source="bls",
                                message="BLS changed a previously stored same-source value",
                                details={
                                    "revision_count": write.revised,
                                    "start_year": start_year,
                                    "end_year": group_end,
                                },
                            )
                        ],
                        run_id=run.run_id,
                    )
                counts = counters[indicator.indicator_id]
                for field in counts:
                    counts[field] += getattr(write, field)
        summary.success_count = len(successes - failures)
        summary.failure_count = len(failures)
        summary.inserted_count = sum(item["inserted"] for item in counters.values())
        summary.revised_count = sum(item["revised"] for item in counters.values())
        summary.results = [
            {"indicator_id": indicator_id, **counts}
            for indicator_id, counts in sorted(counters.items())
        ]
        self._finish_status(summary)
        finish_sync_run(self.session, run, summary)
        return summary

    @staticmethod
    def _add_failure(
        summary: SyncSummary,
        indicator_id: str,
        error: Exception,
        *,
        count: bool = True,
    ) -> None:
        if count:
            summary.failure_count += 1
        summary.errors.append(
            PipelineError(
                indicator_id=indicator_id,
                error_type=type(error).__name__,
                message=str(error),
            )
        )
