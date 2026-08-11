"""Pending mapping validation and DIRECT-only Census data synchronization."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.data_pipeline.repository import (
    finish_sync_run,
    latest_source_observation_date,
    persist_official_observations,
    record_failed_census_request,
    record_successful_census_request,
    start_sync_run,
)
from app.data_pipeline.schemas import (
    CensusBackfillOptions,
    CensusSelection,
    CensusSyncOptions,
    PipelineError,
    PipelineSelection,
    SyncSummary,
)
from app.data_quality.checks import check_official_observation_batch, official_source_issue
from app.data_quality.repository import persist_quality_issues
from app.data_sources.census.adapter import CensusAdapter
from app.data_sources.census.client import CensusClient
from app.data_sources.census.exceptions import CensusError
from app.data_sources.census.metadata import validate_census_metadata
from app.data_sources.census.schemas import CensusMappingIdentity, CensusPayload
from app.database.models import IndicatorRegistry, IndicatorSourceMapping
from app.indicators.repository import get_indicator
from app.settings import Settings
from app.source_mappings.config import load_source_mapping_catalog_for
from app.source_mappings.policy import source_mapping_is_direct, source_mapping_type
from app.source_mappings.repository import list_source_mappings, seed_source_mappings
from app.source_mappings.schemas import SourceMappingDefinition

SelectedMapping = tuple[IndicatorRegistry, IndicatorSourceMapping, SourceMappingDefinition]


def split_census_years(start_year: int, end_year: int, batch_size: int) -> list[tuple[int, int]]:
    """Split an inclusive year range into bounded EITS time windows."""
    if start_year > end_year:
        raise ValueError("start_year must not be after end_year")
    if batch_size < 1:
        raise ValueError("Census year batch size must be positive")
    return [
        (start, min(start + batch_size - 1, end_year))
        for start in range(start_year, end_year + 1, batch_size)
    ]


class CensusPipelineService:
    """Coordinate shared EITS requests while enforcing mapping-role write gates."""

    def __init__(self, session: Session, settings: Settings, client: CensusClient) -> None:
        self.session = session
        self.settings = settings
        self.client = client
        self.catalog = load_source_mapping_catalog_for("census")
        self.definitions = {item.indicator_id: item for item in self.catalog.mappings}

    def _select(
        self,
        selection: PipelineSelection,
        *,
        direct_only: bool = False,
        usable_only: bool = False,
    ) -> list[SelectedMapping]:
        seed_source_mappings(self.session, self.catalog)
        mappings = list_source_mappings(
            self.session,
            source="census",
            indicator_id=selection.indicator_id,
            enabled=True if selection.enabled_only else None,
            limit=100,
        )
        selected: list[SelectedMapping] = []
        for mapping in mappings:
            definition = self.definitions.get(mapping.indicator_id)
            indicator = get_indicator(self.session, mapping.indicator_id)
            if definition is None or indicator is None:
                continue
            if selection.module is not None and indicator.module != selection.module:
                continue
            if selection.enabled_only and not indicator.enabled:
                continue
            if (
                isinstance(selection, CensusSelection)
                and selection.dataset is not None
                and definition.source_dataset != selection.dataset.lower()
            ):
                continue
            if direct_only and not source_mapping_is_direct(definition.source_parameters):
                continue
            if usable_only and mapping.mapping_status not in {"valid", "valid_with_warning"}:
                continue
            selected.append((indicator, mapping, definition))
        return selected

    async def validate_mappings(self, selection: PipelineSelection) -> SyncSummary:
        """Validate seven candidates while keeping every stored status pending_review."""
        selected = self._select(selection)
        direct_count = sum(source_mapping_is_direct(item[2].source_parameters) for item in selected)
        run = start_sync_run(
            self.session,
            "validate_census_mappings",
            selection.model_dump(mode="json"),
            direct_count,
        )
        summary = self._summary(run.run_id, run.operation, direct_count)
        groups = self._group_by_dataset(selected)
        current_year = date.today().year
        for dataset, group in sorted(groups.items()):
            try:
                dataset_metadata, payload = await self.client.get_dataset_metadata(dataset)
                self._record_payload(summary, run.run_id, payload)
                variables_metadata, payload = await self.client.get_variables_metadata(dataset)
                self._record_payload(summary, run.run_id, payload)
                geography = CensusMappingIdentity.model_validate(
                    group[0][2].source_parameters
                ).geography_query
                rows, payload = await self.client.get_data(
                    dataset, time=str(current_year), geography=geography
                )
                self._record_payload(summary, run.run_id, payload)
            except Exception as error:
                record_failed_census_request(
                    self.session,
                    run.run_id,
                    endpoint=f"timeseries/eits/{dataset}",
                    parameters={"dataset": dataset, "time": str(current_year)},
                    error=error,
                )
                self._fail_validation_group(summary, run.run_id, group, error)
                continue
            for indicator, mapping, definition in group:
                validation = validate_census_metadata(
                    definition, dataset_metadata, variables_metadata, rows
                )
                mapping.mapping_status = "pending_review"
                mapping.mapping_warnings_json = [*validation.errors, *validation.warnings]
                mapping.metadata_checked_at = datetime.now(UTC)
                mapping_type = source_mapping_type(definition.source_parameters)
                summary.results.append(
                    {
                        "indicator_id": indicator.indicator_id,
                        "mapping_type": mapping_type,
                        "status": "pending_review",
                        "identity_confirmed": validation.identity_confirmed,
                        "returned_seasonal_values": validation.returned_seasonal_values,
                        "returned_geography_values": validation.returned_geography_values,
                        "errors": validation.errors,
                        "warnings": validation.warnings,
                    }
                )
                issues = [
                    official_source_issue(
                        indicator.indicator_id,
                        event_type=self._event_type(message),
                        severity="error",
                        source="census",
                        message=message,
                    )
                    for message in validation.errors
                ]
                summary.quality_event_count += persist_quality_issues(
                    self.session, issues, run_id=run.run_id
                )
                if mapping_type != "DIRECT":
                    summary.skipped_count += 1
                elif validation.errors:
                    summary.failure_count += 1
                    self._add_failure(
                        summary,
                        indicator.indicator_id,
                        RuntimeError("; ".join(validation.errors)),
                        count=False,
                    )
                else:
                    summary.success_count += 1
        self._finish_status(summary)
        finish_sync_run(self.session, run, summary)
        return summary

    async def backfill(self, options: CensusBackfillOptions) -> SyncSummary:
        """Backfill only live-reviewed DIRECT mappings in small shared dataset windows."""
        if options.start > options.end:
            raise ValueError("start must not be after end")
        selected = self._select(options, direct_only=True, usable_only=True)
        windows = split_census_years(
            options.start.year, options.end.year, self.settings.census_year_batch_size
        )
        return await self._fetch_windows(
            "backfill_census",
            selected,
            windows,
            options.dry_run,
            lower_bound=options.start,
            upper_bound=options.end,
        )

    async def sync(self, options: CensusSyncOptions) -> SyncSummary:
        """Synchronize each DIRECT mapping using its configured program lookback."""
        selected = self._select(options, direct_only=True, usable_only=True)
        current_year = date.today().year
        starts: dict[str, int] = {}
        for indicator, mapping, definition in selected:
            first = int(str(definition.source_parameters["first_available_period"])[:4])
            latest = latest_source_observation_date(
                self.session, indicator.indicator_id, source="census"
            )
            starts[indicator.indicator_id] = (
                max(first, latest.year - mapping.revision_lookback_years)
                if latest is not None
                else max(first, self.settings.census_default_backfill_start_year)
            )
        groups = self._group_by_dataset(selected)
        run = start_sync_run(
            self.session,
            "sync_census",
            {"start_years": starts, "end_year": current_year, "dry_run": options.dry_run},
            len(selected),
        )
        summary = self._summary(run.run_id, run.operation, len(selected))
        totals: dict[str, dict[str, int]] = defaultdict(self._empty_counts)
        successes: set[str] = set()
        failures: set[str] = set()
        for group in groups.values():
            start = min(starts[item[0].indicator_id] for item in group)
            windows = split_census_years(start, current_year, self.settings.census_year_batch_size)
            await self._process_group(
                run.run_id,
                summary,
                group,
                windows,
                options.dry_run,
                totals,
                successes,
                failures,
            )
        self._finalize_fetch(summary, totals, successes, failures)
        finish_sync_run(self.session, run, summary)
        return summary

    async def _fetch_windows(
        self,
        operation: str,
        selected: Sequence[SelectedMapping],
        windows: list[tuple[int, int]],
        dry_run: bool,
        *,
        lower_bound: date | None = None,
        upper_bound: date | None = None,
    ) -> SyncSummary:
        run = start_sync_run(
            self.session,
            operation,
            {"year_windows": windows, "dry_run": dry_run},
            len(selected),
        )
        summary = self._summary(run.run_id, operation, len(selected))
        totals: dict[str, dict[str, int]] = defaultdict(self._empty_counts)
        successes: set[str] = set()
        failures: set[str] = set()
        for group in self._group_by_dataset(selected).values():
            await self._process_group(
                run.run_id,
                summary,
                group,
                windows,
                dry_run,
                totals,
                successes,
                failures,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
            )
        self._finalize_fetch(summary, totals, successes, failures)
        finish_sync_run(self.session, run, summary)
        return summary

    async def _process_group(
        self,
        run_id: UUID,
        summary: SyncSummary,
        group: Sequence[SelectedMapping],
        windows: list[tuple[int, int]],
        dry_run: bool,
        totals: dict[str, dict[str, int]],
        successes: set[str],
        failures: set[str],
        *,
        lower_bound: date | None = None,
        upper_bound: date | None = None,
    ) -> None:
        if not group:
            return
        dataset = group[0][2].source_dataset
        geography = CensusMappingIdentity.model_validate(
            group[0][2].source_parameters
        ).geography_query
        for start_year, end_year in windows:
            active = [
                item
                for item in group
                if end_year >= int(str(item[2].source_parameters["first_available_period"])[:4])
            ]
            if not active:
                continue
            first = min(
                int(str(item[2].source_parameters["first_available_period"])[:4]) for item in active
            )
            request_start = max(date(start_year, 1, 1), date(first, 1, 1))
            if lower_bound is not None:
                request_start = max(request_start, lower_bound)
            request_end = date(end_year, 12, 1)
            if upper_bound is not None:
                request_end = min(request_end, upper_bound)
            if request_start > request_end:
                continue
            time_query = (
                request_start.strftime("%Y-%m")
                if request_start == request_end
                else (f"from {request_start.strftime('%Y-%m')} to {request_end.strftime('%Y-%m')}")
            )
            try:
                rows, payload = await self.client.get_data(
                    dataset, time=time_query, geography=geography
                )
                self._record_payload(summary, run_id, payload)
            except Exception as error:
                record_failed_census_request(
                    self.session,
                    run_id,
                    endpoint=f"timeseries/eits/{dataset}",
                    parameters={"dataset": dataset, "time": time_query, "for": geography},
                    error=error,
                )
                failures.update(item[0].indicator_id for item in active)
                for indicator, _, _ in active:
                    self._add_failure(summary, indicator.indicator_id, error, count=False)
                continue
            for indicator, mapping, definition in active:
                try:
                    identity = CensusMappingIdentity.model_validate(definition.source_parameters)
                    observations = CensusAdapter.observations(rows, identity)
                except Exception as error:
                    failures.add(indicator.indicator_id)
                    self._add_failure(summary, indicator.indicator_id, error, count=False)
                    summary.quality_event_count += persist_quality_issues(
                        self.session,
                        [
                            official_source_issue(
                                indicator.indicator_id,
                                event_type=self._event_type(str(error)),
                                severity="error",
                                source="census",
                                message=str(error),
                            )
                        ],
                        run_id=run_id,
                    )
                    continue
                first_period = date.fromisoformat(
                    f"{identity.first_available_period}-01"
                    if len(identity.first_available_period) == 7
                    else identity.first_available_period
                )
                observations = [item for item in observations if item.date >= first_period]
                if lower_bound is not None:
                    observations = [item for item in observations if item.date >= lower_bound]
                if upper_bound is not None:
                    observations = [item for item in observations if item.date <= upper_bound]
                issues = check_official_observation_batch(
                    indicator.indicator_id, observations, source="census"
                )
                summary.quality_event_count += persist_quality_issues(
                    self.session, issues, run_id=run_id
                )
                if not observations:
                    failures.add(indicator.indicator_id)
                    continue
                write = persist_official_observations(
                    self.session,
                    indicator=indicator,
                    source="census",
                    source_series_id=mapping.source_series_id,
                    observations=observations,
                    payload_hash=payload.response_hash,
                    retrieved_at=payload.finished_at,
                    dry_run=dry_run,
                    batch_size=self.settings.database_batch_size,
                )
                successes.add(indicator.indicator_id)
                counts = totals[indicator.indicator_id]
                for field in counts:
                    counts[field] += getattr(write, field)
                if write.revised:
                    summary.quality_event_count += persist_quality_issues(
                        self.session,
                        [
                            official_source_issue(
                                indicator.indicator_id,
                                event_type="source_revision_detected",
                                severity="info",
                                source="census",
                                message="Census changed a previously stored same-source value",
                                details={
                                    "revision_count": write.revised,
                                    "dataset": dataset,
                                    "time": time_query,
                                },
                            )
                        ],
                        run_id=run_id,
                    )

    def _fail_validation_group(
        self,
        summary: SyncSummary,
        run_id: UUID,
        group: Sequence[SelectedMapping],
        error: Exception,
    ) -> None:
        for indicator, mapping, definition in group:
            mapping.mapping_status = "pending_review"
            mapping.mapping_warnings_json = [
                f"Census validation unavailable: {type(error).__name__}"
            ]
            mapping.metadata_checked_at = datetime.now(UTC)
            if source_mapping_is_direct(definition.source_parameters):
                summary.failure_count += 1
                self._add_failure(summary, indicator.indicator_id, error, count=False)
            else:
                summary.skipped_count += 1
            summary.quality_event_count += persist_quality_issues(
                self.session,
                [
                    official_source_issue(
                        indicator.indicator_id,
                        event_type="source_series_unavailable",
                        severity="warning",
                        source="census",
                        message=f"Census request failed: {type(error).__name__}",
                    )
                ],
                run_id=run_id,
            )

    def _record_payload(self, summary: SyncSummary, run_id: UUID, payload: CensusPayload) -> None:
        record_successful_census_request(self.session, run_id, payload)
        summary.request_count += 1
        summary.cache_hit_count += int(payload.from_cache)

    @staticmethod
    def _group_by_dataset(selected: Sequence[SelectedMapping]) -> dict[str, list[SelectedMapping]]:
        groups: dict[str, list[SelectedMapping]] = defaultdict(list)
        for item in selected:
            groups[item[2].source_dataset].append(item)
        return groups

    @staticmethod
    def _event_type(message: str) -> str:
        for event_type in (
            "unexpected_dataset",
            "unexpected_category",
            "unexpected_data_type",
            "unexpected_geography",
            "unexpected_seasonal_adjustment",
            "unexpected_unit",
        ):
            if event_type in message:
                return event_type
        return "source_metadata_mismatch"

    @staticmethod
    def _empty_counts() -> dict[str, int]:
        return {"inserted": 0, "revised": 0, "unchanged": 0, "missing": 0}

    @staticmethod
    def _summary(run_id: UUID, operation: str, indicator_count: int) -> SyncSummary:
        return SyncSummary(
            run_id=run_id,
            operation=operation,
            status="succeeded",
            indicator_count=indicator_count,
        )

    def _finalize_fetch(
        self,
        summary: SyncSummary,
        totals: dict[str, dict[str, int]],
        successes: set[str],
        failures: set[str],
    ) -> None:
        summary.success_count = len(successes - failures)
        summary.failure_count = len(failures)
        summary.inserted_count = sum(item["inserted"] for item in totals.values())
        summary.revised_count = sum(item["revised"] for item in totals.values())
        summary.results = [
            {"indicator_id": indicator_id, **counts}
            for indicator_id, counts in sorted(totals.items())
        ]
        self._finish_status(summary)

    @staticmethod
    def _finish_status(summary: SyncSummary) -> None:
        if summary.failure_count == 0:
            summary.status = "succeeded"
        elif summary.success_count or summary.skipped_count:
            summary.status = "partial"
        else:
            summary.status = "failed"

    @staticmethod
    def _add_failure(
        summary: SyncSummary,
        indicator_id: str,
        error: Exception,
        *,
        count: bool,
    ) -> None:
        if count:
            summary.failure_count += 1
        summary.errors.append(
            PipelineError(
                indicator_id=indicator_id,
                error_type=type(error).__name__,
                message=(
                    str(error)
                    if isinstance(error, (CensusError, RuntimeError, ValueError))
                    else "Unexpected Census operation failure"
                ),
            )
        )
