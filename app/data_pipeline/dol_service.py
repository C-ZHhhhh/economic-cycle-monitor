"""DOL Weekly Claims validation and source-specific reconciliation."""

from datetime import date

from sqlalchemy.orm import Session

from app.data_pipeline.repository import (
    finish_sync_run,
    persist_official_observations,
    record_failed_official_request,
    record_successful_dol_request,
    start_sync_run,
)
from app.data_pipeline.schemas import BackfillOptions, PipelineError, PipelineSelection, SyncSummary
from app.data_quality.checks import official_source_issue
from app.data_quality.repository import persist_quality_issues
from app.data_sources.dol.client import DolClient
from app.data_sources.dol.exceptions import DolOperationalDegradation
from app.data_sources.dol.schemas import DolMappingIdentity
from app.data_sources.dol.weekly_claims import parse_weekly_claims
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
            "backfill_dol", options, options.start.year, options.end.year, options.dry_run
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
        try:
            payload = await self.client.get_national_weekly_claims(start_year, end_year)
            record_successful_dol_request(self.session, run.run_id, payload)
            summary.request_count = 1
            summary.cache_hit_count = int(payload.from_cache)
        except Exception as error:
            record_failed_official_request(
                self.session,
                run.run_id,
                source="dol",
                endpoint="unemploy/wkclaims/report.asp",
                parameters={"start_year": start_year, "end_year": end_year},
                error=error,
            )
            event = (
                "source_operational_degradation"
                if isinstance(error, DolOperationalDegradation)
                else "source_request_error"
            )
            summary.status = "failed"
            summary.failure_count = len(selected)
            summary.errors = [
                PipelineError(
                    indicator_id=item[0].indicator_id,
                    error_type=event,
                    message=str(error),
                )
                for item in selected
            ]
            issues = [
                official_source_issue(
                    item[0].indicator_id,
                    event_type=event,
                    severity="warning" if isinstance(error, DolOperationalDegradation) else "error",
                    source="dol",
                    message=str(error),
                )
                for item in selected
            ]
            summary.quality_event_count += persist_quality_issues(
                self.session, issues, run_id=run.run_id
            )
            finish_sync_run(self.session, run, summary)
            return summary
        for indicator, mapping, definition in selected:
            identity = DolMappingIdentity.model_validate(definition.source_parameters)
            try:
                observations = parse_weekly_claims(payload.body, identity)
                if recent_only:
                    observations = observations[-self.settings.dol_recent_observation_periods :]
            except Exception as error:
                # Missing candidate paths remain pending, never invalid. Live evidence
                # is reported for an explicit human mapping decision.
                mapping.mapping_status = "pending_review"
                mapping.mapping_warnings_json = [str(error)]
                summary.failure_count += 1
                summary.errors.append(
                    PipelineError(
                        indicator_id=indicator.indicator_id,
                        error_type=type(error).__name__,
                        message=str(error),
                    )
                )
                continue
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
            summary.success_count += 1
            summary.inserted_count += result.inserted
            summary.revised_count += result.revised
            summary.results.append({"indicator_id": indicator.indicator_id, **result.model_dump()})
        if summary.failure_count:
            summary.status = "failed" if not summary.success_count else "partial"
        finish_sync_run(self.session, run, summary)
        return summary
