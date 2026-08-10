"""Strict BEA NIPA mapping validation, history, and incremental synchronization."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.data_pipeline.repository import (
    finish_sync_run,
    latest_source_observation_date,
    persist_official_observations,
    record_failed_bea_request,
    record_successful_bea_request,
    start_sync_run,
)
from app.data_pipeline.schemas import (
    BeaBackfillOptions,
    BeaSyncOptions,
    PipelineError,
    PipelineSelection,
    SyncSummary,
)
from app.data_quality.checks import (
    QualityIssue,
    check_official_observation_batch,
    official_source_issue,
)
from app.data_quality.repository import persist_quality_issues, reconcile_quality_issues
from app.data_sources.bea.adapter import BeaAdapter
from app.data_sources.bea.client import BeaClient
from app.data_sources.bea.exceptions import BeaBusinessError, BeaError
from app.data_sources.bea.metadata import BeaMetadataValidation, validate_bea_metadata
from app.data_sources.bea.schemas import BeaNipaDataRow, BeaParameterValue, BeaPayload
from app.database.models import IndicatorRegistry, IndicatorSourceMapping
from app.indicators.repository import get_indicator
from app.settings import Settings
from app.source_mappings.config import load_source_mapping_catalog_for
from app.source_mappings.repository import list_source_mappings, seed_source_mappings
from app.source_mappings.schemas import SourceMappingDefinition

SelectedMapping = tuple[IndicatorRegistry, IndicatorSourceMapping, SourceMappingDefinition]


def split_bea_years(start_year: int, end_year: int, batch_size: int) -> list[list[int]]:
    """Split an inclusive year range into small explicit BEA request batches."""
    if start_year > end_year:
        raise ValueError("start_year must not be after end_year")
    if batch_size < 1:
        raise ValueError("BEA year batch size must be positive")
    years = list(range(start_year, end_year + 1))
    return [years[index : index + batch_size] for index in range(0, len(years), batch_size)]


def bea_year_is_available(
    values: list[BeaParameterValue], *, table_name: str, frequency: str, year: int
) -> bool:
    """Check BEA table/frequency year bounds returned by GetParameterValues."""
    field_names = {
        "A": ("FirstAnnualYear", "LastAnnualYear"),
        "Q": ("FirstQuarterlyYear", "LastQuarterlyYear"),
        "M": ("FirstMonthlyYear", "LastMonthlyYear"),
    }
    fields = field_names.get(frequency.upper())
    if fields is None:
        return False
    for item in values:
        if item.Key != table_name:
            continue
        first = getattr(item, fields[0])
        last = getattr(item, fields[1])
        try:
            return first is not None and last is not None and int(first) <= year <= int(last)
        except ValueError:
            return False
    return False


class BeaPipelineService:
    """Coordinate bounded BEA table requests with per-candidate write gates."""

    def __init__(self, session: Session, settings: Settings, client: BeaClient) -> None:
        self.session = session
        self.settings = settings
        self.client = client
        self.catalog = load_source_mapping_catalog_for("bea")
        self.definitions = {item.indicator_id: item for item in self.catalog.mappings}

    def _select(self, selection: PipelineSelection) -> list[SelectedMapping]:
        seed_source_mappings(self.session, self.catalog)
        mappings = list_source_mappings(
            self.session,
            source="bea",
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
            selected.append((indicator, mapping, definition))
        return selected

    async def validate_mappings(self, selection: PipelineSelection) -> SyncSummary:
        """Discover NIPA parameters, then validate each exact candidate from GetData."""
        selected = self._select(selection)
        run = start_sync_run(
            self.session,
            "validate_bea_mappings",
            selection.model_dump(mode="json"),
            len(selected),
        )
        summary = self._summary(run.run_id, run.operation, len(selected))
        if not selected:
            finish_sync_run(self.session, run, summary)
            return summary
        try:
            datasets, payload = await self.client.get_dataset_list()
            self._record_payload(summary, run.run_id, payload)
            if "NIPA" not in {item.DatasetName for item in datasets}:
                raise RuntimeError("BEA dataset discovery did not return NIPA")

            parameters, payload = await self.client.get_parameter_list("NIPA")
            self._record_payload(summary, run.run_id, payload)
            required = {"TableName", "Frequency", "Year"}
            returned = {item.ParameterName for item in parameters}
            if not required.issubset(returned):
                raise RuntimeError("BEA NIPA parameter discovery omitted required parameters")

            table_values, payload = await self.client.get_parameter_values("TableName")
            self._record_payload(summary, run.run_id, payload)
            table_keys = {item.Key for item in table_values}
            expected_tables = {
                str(definition.source_parameters["table_name"]) for _, _, definition in selected
            }
            if not expected_tables.issubset(table_keys):
                missing = sorted(expected_tables - table_keys)
                raise RuntimeError(f"BEA parameter discovery omitted candidate tables {missing}")

            frequency_values, payload = await self.client.get_parameter_values("Frequency")
            self._record_payload(summary, run.run_id, payload)
            expected_frequencies = {
                str(definition.source_parameters["frequency"]) for _, _, definition in selected
            }
            if not expected_frequencies.issubset({item.Key for item in frequency_values}):
                raise RuntimeError("BEA parameter discovery omitted a candidate frequency")

            year_values, payload = await self.client.get_parameter_values("Year")
            self._record_payload(summary, run.run_id, payload)
            current_year = date.today().year
            unavailable = [
                (
                    str(definition.source_parameters["table_name"]),
                    str(definition.source_parameters["frequency"]),
                )
                for _, _, definition in selected
                if not bea_year_is_available(
                    year_values,
                    table_name=str(definition.source_parameters["table_name"]),
                    frequency=str(definition.source_parameters["frequency"]),
                    year=current_year,
                )
            ]
            if unavailable:
                raise RuntimeError(
                    f"BEA parameter discovery omitted current-year coverage for {unavailable}"
                )
        except Exception as error:
            self._record_discovery_failure(run.run_id, error)
            self._fail_group(summary, selected, error, run.run_id)
            self._finish_status(summary)
            finish_sync_run(self.session, run, summary)
            return summary

        for group in self._group_by_table_frequency(selected).values():
            definition = group[0][2]
            table_name = str(definition.source_parameters["table_name"])
            frequency = str(definition.source_parameters["frequency"])
            try:
                rows, _, payload = await self.client.get_nipa_data(
                    table_name=table_name,
                    frequency=frequency,
                    years=[date.today().year],
                )
                self._record_payload(summary, run.run_id, payload)
            except Exception as error:
                self._record_data_failure(
                    run.run_id, table_name, frequency, [date.today().year], error
                )
                self._fail_group(summary, group, error, run.run_id)
                continue
            for indicator, mapping, candidate in group:
                validation = validate_bea_metadata(candidate, rows)
                self._apply_validation(mapping, validation)
                summary.results.append(self._validation_result(candidate, validation))
                summary.quality_event_count += self._reconcile_validation_issues(
                    run.run_id, indicator.indicator_id, validation
                )
                if validation.errors:
                    summary.failure_count += 1
                else:
                    summary.success_count += 1
        self._finish_status(summary)
        finish_sync_run(self.session, run, summary)
        return summary

    async def backfill(self, options: BeaBackfillOptions) -> SyncSummary:
        """Fetch explicit BEA year batches and write only exact validated candidates."""
        selected = self._select(options)
        batches = split_bea_years(
            options.start_year, options.end_year, self.settings.bea_year_batch_size
        )
        return await self._fetch_batches("backfill_bea", selected, batches, options.dry_run)

    async def sync(self, options: BeaSyncOptions) -> SyncSummary:
        """Refetch configured same-source lookbacks so revisions remain auditable."""
        selected = self._select(options)
        current_year = date.today().year
        start_years: dict[str, int] = {}
        for item in selected:
            indicator, mapping, _ = item
            latest = latest_source_observation_date(
                self.session, indicator.indicator_id, source="bea"
            )
            start_year = (
                max(
                    self.settings.bea_default_backfill_start_year,
                    latest.year - mapping.revision_lookback_years,
                )
                if latest is not None
                else self.settings.bea_default_backfill_start_year
            )
            start_years[indicator.indicator_id] = start_year
        run = start_sync_run(
            self.session,
            "sync_bea",
            {
                "start_years": start_years,
                "end_year": current_year,
                "dry_run": options.dry_run,
            },
            len(selected),
        )
        summary = self._summary(run.run_id, run.operation, len(selected))
        totals: dict[str, dict[str, int]] = defaultdict(self._empty_counts)
        successes: set[str] = set()
        failures: set[str] = set()
        for _, items in sorted(self._group_by_table_frequency(selected).items()):
            start_year = min(start_years[item[0].indicator_id] for item in items)
            batches = split_bea_years(start_year, current_year, self.settings.bea_year_batch_size)
            await self._process_batches(
                run.run_id,
                summary,
                items,
                batches,
                options.dry_run,
                totals,
                successes,
                failures,
            )
        self._finalize_fetch(summary, totals, successes, failures)
        finish_sync_run(self.session, run, summary)
        return summary

    async def _fetch_batches(
        self,
        operation: str,
        selected: Sequence[SelectedMapping],
        batches: list[list[int]],
        dry_run: bool,
    ) -> SyncSummary:
        run = start_sync_run(
            self.session,
            operation,
            {
                "year_batches": batches,
                "dry_run": dry_run,
            },
            len(selected),
        )
        summary = self._summary(run.run_id, operation, len(selected))
        totals: dict[str, dict[str, int]] = defaultdict(self._empty_counts)
        successes: set[str] = set()
        failures: set[str] = set()
        await self._process_batches(
            run.run_id,
            summary,
            selected,
            batches,
            dry_run,
            totals,
            successes,
            failures,
        )
        self._finalize_fetch(summary, totals, successes, failures)
        finish_sync_run(self.session, run, summary)
        return summary

    async def _process_batches(
        self,
        run_id: UUID,
        summary: SyncSummary,
        selected: Sequence[SelectedMapping],
        batches: list[list[int]],
        dry_run: bool,
        totals: dict[str, dict[str, int]],
        successes: set[str],
        failures: set[str],
    ) -> None:
        for group in self._group_by_table_frequency(selected).values():
            definition = group[0][2]
            table_name = str(definition.source_parameters["table_name"])
            frequency = str(definition.source_parameters["frequency"])
            for years in batches:
                active_group = [
                    item
                    for item in group
                    if any(
                        year >= int(item[2].source_parameters.get("first_available_year", 1929))
                        for year in years
                    )
                ]
                if not active_group:
                    continue
                first_available_year = min(
                    int(item[2].source_parameters.get("first_available_year", 1929))
                    for item in active_group
                )
                request_years = [year for year in years if year >= first_available_year]
                try:
                    rows, notes, payload = await self.client.get_nipa_data(
                        table_name=table_name, frequency=frequency, years=request_years
                    )
                    self._record_payload(summary, run_id, payload)
                except Exception as error:
                    self._record_data_failure(run_id, table_name, frequency, request_years, error)
                    self._fail_group(
                        summary,
                        active_group,
                        error,
                        run_id,
                        count_failures=False,
                        years=request_years,
                    )
                    failures.update(indicator.indicator_id for indicator, _, _ in active_group)
                    continue
                for indicator, mapping, candidate in active_group:
                    validation = validate_bea_metadata(candidate, rows)
                    self._apply_validation(mapping, validation)
                    if validation.errors:
                        failures.add(indicator.indicator_id)
                        self._add_failure(
                            summary,
                            indicator.indicator_id,
                            RuntimeError("; ".join(validation.errors)),
                            count=False,
                        )
                        summary.quality_event_count += self._persist_validation_issues(
                            run_id, indicator.indicator_id, validation
                        )
                        continue
                    target_rows = self._matching_rows(candidate, rows)
                    observations = BeaAdapter.observations(
                        target_rows,
                        notes,
                        frequency=frequency,
                        target_unit_multiplier=int(
                            candidate.source_parameters["target_unit_multiplier"]
                        ),
                    )
                    issues = check_official_observation_batch(
                        indicator.indicator_id, observations, source="bea"
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
                        source="bea",
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
                                    source="bea",
                                    message="BEA changed a previously stored same-source value",
                                    details={
                                        "revision_count": write.revised,
                                        "years": request_years,
                                    },
                                )
                            ],
                            run_id=run_id,
                        )

    def _reconcile_validation_issues(
        self, run_id: UUID, indicator_id: str, validation: BeaMetadataValidation
    ) -> int:
        issues = self._validation_issues(indicator_id, validation)
        inserted, _resolved = reconcile_quality_issues(
            self.session,
            issues,
            run_id=run_id,
            indicator_id=indicator_id,
            event_types=(
                "source_metadata_mismatch",
                "unexpected_table_line",
                "unexpected_series_code",
                "invalid_unit_multiplier",
            ),
        )
        return inserted

    def _persist_validation_issues(
        self, run_id: UUID, indicator_id: str, validation: BeaMetadataValidation
    ) -> int:
        return persist_quality_issues(
            self.session,
            self._validation_issues(indicator_id, validation),
            run_id=run_id,
        )

    @staticmethod
    def _validation_issues(
        indicator_id: str, validation: BeaMetadataValidation
    ) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        for message in validation.errors:
            event_type = "source_metadata_mismatch"
            if message.startswith("LineNumber") or message.startswith("TableName"):
                event_type = "unexpected_table_line"
            elif message.startswith("SeriesCode"):
                event_type = "unexpected_series_code"
            elif "UNIT_MULT" in message or "unit metadata" in message:
                event_type = "invalid_unit_multiplier"
            issues.append(
                official_source_issue(
                    indicator_id,
                    event_type=event_type,
                    severity="error",
                    source="bea",
                    message=message,
                )
            )
        return issues

    def _record_payload(self, summary: SyncSummary, run_id: UUID, payload: BeaPayload) -> None:
        record_successful_bea_request(self.session, run_id, payload)
        summary.request_count += 1
        summary.cache_hit_count += int(payload.from_cache)

    def _record_discovery_failure(self, run_id: UUID, error: Exception) -> None:
        record_failed_bea_request(
            self.session,
            run_id,
            endpoint="metadata_discovery",
            parameters={"DataSetName": "NIPA"},
            error=error,
        )

    def _record_data_failure(
        self,
        run_id: UUID,
        table_name: str,
        frequency: str,
        years: list[int],
        error: Exception,
    ) -> None:
        record_failed_bea_request(
            self.session,
            run_id,
            endpoint="GetData",
            parameters={
                "DataSetName": "NIPA",
                "TableName": table_name,
                "Frequency": frequency,
                "Year": ",".join(str(year) for year in years),
            },
            error=error,
        )

    def _fail_group(
        self,
        summary: SyncSummary,
        group: Sequence[SelectedMapping],
        error: Exception,
        run_id: UUID,
        *,
        count_failures: bool = True,
        years: list[int] | None = None,
    ) -> None:
        summary.request_count += 1
        for indicator, mapping, _ in group:
            mapping.mapping_status = "pending_review"
            mapping.mapping_warnings_json = [f"BEA validation unavailable: {type(error).__name__}"]
            mapping.metadata_checked_at = datetime.now(UTC)
            self._add_failure(summary, indicator.indicator_id, error, count=count_failures)
            summary.quality_event_count += persist_quality_issues(
                self.session,
                [
                    official_source_issue(
                        indicator.indicator_id,
                        event_type=(
                            "source_business_error"
                            if isinstance(error, BeaBusinessError)
                            else "source_series_unavailable"
                        ),
                        severity="warning",
                        source="bea",
                        message=f"BEA request failed: {type(error).__name__}",
                        details={"years": years} if years is not None else {},
                    )
                ],
                run_id=run_id,
            )

    @staticmethod
    def _matching_rows(
        definition: SourceMappingDefinition, rows: list[BeaNipaDataRow]
    ) -> list[BeaNipaDataRow]:
        parameters = definition.source_parameters
        expected_line = Decimal(str(parameters["line_number"]))
        matches: list[BeaNipaDataRow] = []
        for row in rows:
            try:
                line = Decimal(row.LineNumber)
            except InvalidOperation:
                continue
            if (
                row.TableName == parameters["table_name"]
                and row.SeriesCode == definition.source_series_id
                and line == expected_line
            ):
                matches.append(row)
        return matches

    @staticmethod
    def _group_by_table_frequency(
        selected: Sequence[SelectedMapping],
    ) -> dict[tuple[str, str], list[SelectedMapping]]:
        groups: dict[tuple[str, str], list[SelectedMapping]] = defaultdict(list)
        for item in selected:
            parameters = item[2].source_parameters
            groups[(str(parameters["table_name"]), str(parameters["frequency"]))].append(item)
        return groups

    @staticmethod
    def _apply_validation(
        mapping: IndicatorSourceMapping, validation: BeaMetadataValidation
    ) -> None:
        mapping.mapping_status = validation.status
        mapping.mapping_warnings_json = [*validation.errors, *validation.warnings]
        mapping.metadata_checked_at = datetime.now(UTC)

    @staticmethod
    def _validation_result(
        definition: SourceMappingDefinition, validation: BeaMetadataValidation
    ) -> dict[str, Any]:
        return {
            "indicator_id": definition.indicator_id,
            "source_series_id": definition.source_series_id,
            "table_name": definition.source_parameters["table_name"],
            "candidate_line_number": definition.source_parameters["line_number"],
            "returned_line_numbers": validation.returned_line_numbers,
            "status": validation.status,
            "identity_confirmed": validation.identity_confirmed,
            "errors": validation.errors,
            "warnings": validation.warnings,
        }

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
        count: bool = True,
    ) -> None:
        if count:
            summary.failure_count += 1
        summary.errors.append(
            PipelineError(
                indicator_id=indicator_id,
                error_type=type(error).__name__,
                message=(
                    str(error)
                    if isinstance(error, (BeaError, RuntimeError, ValueError))
                    else "Unexpected BEA operation failure"
                ),
            )
        )
