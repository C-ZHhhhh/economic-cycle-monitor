"""Federal Reserve Board mapping validation and release reconciliation."""

import re
from calendar import month_name
from collections.abc import Sequence
from datetime import UTC, date, datetime
from html import unescape

from sqlalchemy.orm import Session

from app.data_pipeline.repository import (
    finish_sync_run,
    persist_official_observations,
    record_failed_official_request,
    record_successful_federal_reserve_request,
    start_sync_run,
)
from app.data_pipeline.schemas import BackfillOptions, PipelineError, PipelineSelection, SyncSummary
from app.data_quality.checks import official_source_issue
from app.data_quality.repository import persist_quality_issues
from app.data_sources.federal_reserve.client import FederalReserveClient
from app.data_sources.federal_reserve.exceptions import (
    FederalReserveBaseYearError,
)
from app.data_sources.federal_reserve.g17 import parse_g17_text, validate_g17_identity
from app.data_sources.federal_reserve.g19 import parse_g19_zip
from app.data_sources.federal_reserve.schemas import (
    FederalReserveMappingIdentity,
    FederalReservePayload,
)
from app.data_sources.official.schemas import OfficialObservation
from app.database.models import IndicatorRegistry, IndicatorSourceMapping
from app.indicators.repository import get_indicator
from app.settings import Settings
from app.source_mappings.config import load_source_mapping_catalog_for
from app.source_mappings.repository import list_source_mappings, seed_source_mappings
from app.source_mappings.schemas import SourceMappingDefinition

Selected = tuple[IndicatorRegistry, IndicatorSourceMapping, SourceMappingDefinition]
_DATE = re.compile(r"(20\d{2})[-/]([01]?\d)[-/]([0-3]?\d)")
_NAMED_RELEASE_DATE = re.compile(
    r"release\s+date\s*\*?\s*:?\s*([A-Za-z]+)\s+([0-3]?\d),\s*(20\d{2})",
    re.IGNORECASE,
)


class FederalReservePipelineService:
    """Use Board static releases without any DDP package dependency."""

    def __init__(self, session: Session, settings: Settings, client: FederalReserveClient) -> None:
        self.session = session
        self.settings = settings
        self.client = client
        self.catalog = load_source_mapping_catalog_for("federal_reserve")
        self.definitions = {item.indicator_id: item for item in self.catalog.mappings}

    def _select(self, selection: PipelineSelection, *, usable: bool = False) -> list[Selected]:
        seed_source_mappings(self.session, self.catalog)
        rows = list_source_mappings(
            self.session,
            source="federal_reserve",
            indicator_id=selection.indicator_id,
            enabled=True if selection.enabled_only else None,
            limit=20,
        )
        selected: list[Selected] = []
        for mapping in rows:
            indicator = get_indicator(self.session, mapping.indicator_id)
            definition = self.definitions.get(mapping.indicator_id)
            if indicator is None or definition is None:
                continue
            if selection.module and indicator.module != selection.module:
                continue
            if usable and mapping.mapping_status not in {"valid", "valid_with_warning"}:
                continue
            selected.append((indicator, mapping, definition))
        return selected

    async def validate_mappings(self, selection: PipelineSelection) -> SyncSummary:
        """Validate official identity evidence without promoting pending candidates."""
        selected = self._select(selection)
        return await self._run("validate_federal_reserve_mappings", selected, None, None, False)

    async def backfill(self, options: BackfillOptions) -> SyncSummary:
        """Reconcile full reviewed release snapshots within a requested date range."""
        return await self._run(
            "backfill_federal_reserve",
            self._select(options, usable=True),
            options.start,
            options.end,
            options.dry_run,
        )

    async def sync(self, selection: PipelineSelection, *, dry_run: bool = False) -> SyncSummary:
        """Reconcile complete G.17 and target G.19 histories for reviewed mappings."""
        return await self._run(
            "sync_federal_reserve",
            self._select(selection, usable=True),
            None,
            None,
            dry_run,
        )

    async def _run(
        self,
        operation: str,
        selected: Sequence[Selected],
        start: date | None,
        end: date | None,
        dry_run: bool,
    ) -> SyncSummary:
        run = start_sync_run(
            self.session,
            operation,
            {"start": start, "end": end, "dry_run": dry_run},
            len(selected),
        )
        summary = SyncSummary(
            run_id=run.run_id,
            operation=operation,
            status="succeeded",
            indicator_count=len(selected),
        )
        by_release: dict[str, list[Selected]] = {}
        for item in selected:
            identity = FederalReserveMappingIdentity.model_validate(item[2].source_parameters)
            by_release.setdefault(identity.release, []).append(item)
        for release, group in by_release.items():
            try:
                parsed, payloads = await self._fetch_release(release, group)
                for payload in payloads:
                    record_successful_federal_reserve_request(self.session, run.run_id, payload)
                    summary.request_count += 1
                    summary.cache_hit_count += int(payload.from_cache)
            except Exception as error:
                record_failed_official_request(
                    self.session,
                    run.run_id,
                    source="federal_reserve",
                    endpoint=f"{release.lower()}-official-release",
                    parameters={"release": release},
                    error=error,
                )
                if isinstance(error, FederalReserveBaseYearError):
                    issues = [
                        official_source_issue(
                            indicator.indicator_id,
                            event_type="unexpected_base_year",
                            severity="error",
                            source="federal_reserve",
                            message=str(error),
                        )
                        for indicator, _, _ in group
                    ]
                    summary.quality_event_count += persist_quality_issues(
                        self.session, issues, run_id=run.run_id
                    )
                for indicator, mapping, _ in group:
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
            for indicator, mapping, definition in group:
                observations = parsed[indicator.indicator_id]
                if start:
                    observations = [item for item in observations if item.date >= start]
                if end:
                    observations = [item for item in observations if item.date <= end]
                result = persist_official_observations(
                    self.session,
                    indicator=indicator,
                    source="federal_reserve",
                    source_series_id=definition.source_series_id,
                    observations=observations,
                    payload_hash=payloads[-1].response_hash,
                    retrieved_at=payloads[-1].finished_at,
                    dry_run=dry_run or operation.startswith("validate_"),
                )
                # Validation is evidence collection only; a human Live Gate promotes status.
                if operation.startswith("validate_"):
                    mapping.mapping_status = "pending_review"
                    mapping.metadata_checked_at = datetime.now(UTC)
                summary.success_count += 1
                summary.inserted_count += result.inserted
                summary.revised_count += result.revised
                summary.results.append(
                    {
                        "indicator_id": indicator.indicator_id,
                        "mapping_status": mapping.mapping_status,
                        **result.model_dump(),
                    }
                )
        if summary.failure_count:
            summary.status = "failed" if not summary.success_count else "partial"
        finish_sync_run(self.session, run, summary)
        return summary

    async def _fetch_release(
        self, release: str, group: Sequence[Selected]
    ) -> tuple[dict[str, list[OfficialObservation]], list[FederalReservePayload]]:
        if release == "G17":
            parsed: dict[str, list[OfficialObservation]] = {}
            payloads: list[FederalReservePayload] = []
            for indicator, _, definition in group:
                identity = FederalReserveMappingIdentity.model_validate(
                    definition.source_parameters
                )
                metadata = await self.client.get_html(identity.metadata_path or "")
                documentation = await self.client.get_text(identity.documentation_path or "")
                data = await self.client.get_text(identity.data_path)
                payloads.extend((metadata, documentation, data))
                base_year, released = validate_g17_identity(
                    identity,
                    documentation.body.decode("utf-8-sig"),
                    metadata.body.decode("utf-8-sig"),
                )
                parsed[indicator.indicator_id] = parse_g17_text(
                    data.body, identity, base_year=base_year, release_date=released
                )
            return parsed, payloads
        identities = [
            FederalReserveMappingIdentity.model_validate(item[2].source_parameters)
            for item in group
        ]
        metadata = await self.client.get_html(identities[0].documentation_path or "")
        release_date = self._release_date(metadata.body.decode("utf-8-sig"))
        data = await self.client.get_xml_zip(identities[0].data_path)
        by_series = parse_g19_zip(
            data.body,
            identities,
            max_members=self.settings.federal_reserve_zip_max_members,
            max_uncompressed_bytes=self.settings.federal_reserve_zip_max_uncompressed_bytes,
            release_date=release_date,
        )
        parsed = {
            indicator.indicator_id: by_series[definition.source_series_id]
            for indicator, _, definition in group
        }
        return parsed, [metadata, data]

    @staticmethod
    def _release_date(text: str) -> date | None:
        normalized = " ".join(unescape(re.sub(r"<[^>]+>", " ", text)).split())
        named = _NAMED_RELEASE_DATE.search(normalized)
        if named:
            months = {name.casefold(): number for number, name in enumerate(month_name) if name}
            month = months.get(named.group(1).casefold())
            if month is not None:
                return date(int(named.group(3)), month, int(named.group(2)))
        match = _DATE.search(normalized)
        return date(*(int(match.group(index)) for index in (1, 2, 3))) if match else None
