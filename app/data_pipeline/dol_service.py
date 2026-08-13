"""DOL Weekly Claims validation and source-specific reconciliation."""

from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session

from app.data_pipeline.repository import (
    finish_sync_run,
    persist_official_observations,
    record_failed_official_request,
    record_successful_dol_request,
    start_sync_run,
)
from app.data_pipeline.schemas import (
    BackfillOptions,
    ObservationWriteResult,
    PipelineError,
    PipelineSelection,
    SyncSummary,
)
from app.data_quality.checks import official_source_issue
from app.data_quality.repository import persist_quality_issues
from app.data_sources.dol.client import DolClient
from app.data_sources.dol.exceptions import DolOperationalDegradation
from app.data_sources.dol.schemas import DolMappingIdentity, DolPayload
from app.data_sources.dol.weekly_claims import parse_weekly_claims
from app.data_sources.official.schemas import OfficialObservation
from app.database.models import IndicatorRegistry, IndicatorSourceMapping
from app.indicators.repository import get_indicator
from app.settings import Settings
from app.source_mappings.config import load_source_mapping_catalog_for
from app.source_mappings.repository import list_source_mappings, seed_source_mappings
from app.source_mappings.schemas import SourceMappingDefinition

Selected = tuple[IndicatorRegistry, IndicatorSourceMapping, SourceMappingDefinition]


class DolPipelineService:
    """Keep DOL operational failures distinct from mapping identity decisions."""

    def __init__(self, session: Session, settings: Settings, client: DolClient) -> None:
        self.session = session
        self.settings = settings
        self.client = client
        self.catalog = load_source_mapping_catalog_for("dol")
        self.definitions = {item.indicator_id: item for item in self.catalog.mappings}

    async def validate_mappings(self, selection: PipelineSelection) -> SyncSummary:
        """Fetch schema evidence while keeping unreviewed XML paths pending."""
        return await self._run(
            "validate_dol_mappings", selection, date.today().year, date.today().year, True
        )

    async def backfill(self, options: BackfillOptions) -> SyncSummary:
        """Fetch reviewed Weekly Claims history using explicit source years."""
        return await self._run(
            "backfill_dol",
            options,
            options.start.year,
            options.end.year,
            options.dry_run,
            start_date=options.start,
            end_date=options.end,
        )

    async def sync(
        self,
        selection: PipelineSelection,
        *,
        dry_run: bool = False,
        annual_reconciliation: bool = False,
    ) -> SyncSummary:
        """Reconcile recent periods or an explicitly requested configured annual window."""
        end_year = date.today().year
        start_year = (
            end_year - self.settings.dol_annual_reconciliation_years + 1
            if annual_reconciliation
            else end_year - 1
        )
        return await self._run(
            "sync_dol_annual_reconciliation" if annual_reconciliation else "sync_dol",
            selection,
            start_year,
            end_year,
            dry_run,
            recent_only=not annual_reconciliation,
        )

    async def _run(
        self,
        operation: str,
        selection: PipelineSelection,
        start_year: int,
        end_year: int,
        dry_run: bool,
        *,
        recent_only: bool = False,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> SyncSummary:
        seed_source_mappings(self.session, self.catalog)
        rows = list_source_mappings(
            self.session,
            source="dol",
            indicator_id=selection.indicator_id,
            enabled=True if selection.enabled_only else None,
            limit=10,
        )
        selected: list[Selected] = []
        for row in rows:
            definition = self.definitions.get(row.indicator_id)
            indicator = get_indicator(self.session, row.indicator_id)
            usable = row.mapping_status in {"valid", "valid_with_warning"}
            if definition is None or indicator is None:
                continue
            if not operation.startswith("validate_") and not usable:
                continue
            selected.append((indicator, row, definition))
        run = start_sync_run(
            self.session,
            operation,
            {"start_year": start_year, "end_year": end_year, "dry_run": dry_run},
            len(selected),
        )
        summary = SyncSummary(
            run_id=run.run_id,
            operation=operation,
            status="succeeded",
            indicator_count=len(selected),
        )
        observations_by_indicator: dict[str, list[tuple[OfficialObservation, DolPayload]]] = {
            item[0].indicator_id: [] for item in selected
        }
        failed_indicators: set[str] = set()
        for year in range(start_year, end_year + 1):
            try:
                payload = await self.client.get_national_weekly_claims(year, year)
                record_successful_dol_request(self.session, run.run_id, payload)
                summary.request_count += 1
                summary.cache_hit_count += int(payload.from_cache)
            except Exception as error:
                self._record_request_failure(run.run_id, year, selected, summary, error)
                failed_indicators.update(item[0].indicator_id for item in selected)
                continue
            for indicator, mapping, definition in selected:
                identity = DolMappingIdentity.model_validate(definition.source_parameters)
                try:
                    observations = parse_weekly_claims(payload.body, identity)
                except Exception as error:
                    mapping.mapping_status = "pending_review"
                    mapping.mapping_warnings_json = [str(error)]
                    failed_indicators.add(indicator.indicator_id)
                    summary.errors.append(
                        PipelineError(
                            indicator_id=indicator.indicator_id,
                            error_type=type(error).__name__,
                            message=f"{year}: {error}",
                        )
                    )
                    continue
                observations_by_indicator[indicator.indicator_id].extend(
                    (observation, payload)
                    for observation in observations
                    if (start_date is None or observation.date >= start_date)
                    and (end_date is None or observation.date <= end_date)
                )
        successful_indicators: set[str] = set()
        for indicator, _, definition in selected:
            gathered = sorted(
                observations_by_indicator[indicator.indicator_id],
                key=lambda item: item[0].date,
            )
            if recent_only:
                gathered = gathered[-self.settings.dol_recent_observation_periods :]
            aggregate = ObservationWriteResult()
            for payload, observations in _group_by_payload(gathered):
                result = persist_official_observations(
                    self.session,
                    indicator=indicator,
                    source="dol",
                    source_series_id=definition.source_series_id,
                    observations=observations,
                    payload_hash=payload.response_hash,
                    retrieved_at=payload.finished_at,
                    dry_run=dry_run,
                )
                aggregate.inserted += result.inserted
                aggregate.revised += result.revised
                aggregate.unchanged += result.unchanged
                aggregate.missing += result.missing
            if gathered:
                successful_indicators.add(indicator.indicator_id)
                summary.inserted_count += aggregate.inserted
                summary.revised_count += aggregate.revised
                summary.results.append(
                    {"indicator_id": indicator.indicator_id, **aggregate.model_dump()}
                )
        summary.failure_count = len(failed_indicators)
        summary.success_count = len(successful_indicators - failed_indicators)
        if summary.failure_count:
            summary.status = "failed" if not summary.success_count else "partial"
        finish_sync_run(self.session, run, summary)
        return summary

    def _record_request_failure(
        self,
        run_id: UUID,
        year: int,
        selected: list[Selected],
        summary: SyncSummary,
        error: Exception,
    ) -> None:
        """Persist one safe annual-request failure and its quality evidence."""
        record_failed_official_request(
            self.session,
            run_id,
            source="dol",
            endpoint="unemploy/wkclaims/report.asp",
            parameters={"start_year": year, "end_year": year},
            error=error,
        )
        event = (
            "source_operational_degradation"
            if isinstance(error, DolOperationalDegradation)
            else "source_request_error"
        )
        summary.errors.extend(
            PipelineError(
                indicator_id=item[0].indicator_id,
                error_type=event,
                message=f"{year}: {error}",
            )
            for item in selected
        )
        issues = [
            official_source_issue(
                item[0].indicator_id,
                event_type=event,
                severity="warning" if isinstance(error, DolOperationalDegradation) else "error",
                source="dol",
                message=f"{year}: {error}",
            )
            for item in selected
        ]
        summary.quality_event_count += persist_quality_issues(self.session, issues, run_id=run_id)


def _group_by_payload(
    gathered: list[tuple[OfficialObservation, DolPayload]],
) -> list[tuple[DolPayload, list[OfficialObservation]]]:
    """Group observations by annual response while preserving request audit hashes."""
    grouped: dict[str, tuple[DolPayload, list[OfficialObservation]]] = {}
    for observation, payload in gathered:
        item = grouped.setdefault(payload.response_hash, (payload, []))
        item[1].append(observation)
    return list(grouped.values())
