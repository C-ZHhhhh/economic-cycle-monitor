"""PostgreSQL gates for public-source metadata and operational failures."""

from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_pipeline.dol_service import DolPipelineService
from app.data_pipeline.fed_service import FederalReservePipelineService
from app.data_pipeline.schemas import BackfillOptions, PipelineSelection
from app.data_sources.dol.client import DolClient
from app.data_sources.federal_reserve.client import FederalReserveClient
from app.data_sources.official.storage import OfficialFileCache, OfficialRawStore
from app.database.models import DataQualityEvent, IndicatorSourceMapping, RawObservation
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.settings import Settings
from app.source_mappings.config import load_source_mapping_catalog_for
from app.source_mappings.repository import seed_source_mappings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _dol(tmp_path: Path, handler: httpx.MockTransport) -> DolClient:
    return DolClient(
        base_url="https://offline.dol.test",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        cache=OfficialFileCache(
            tmp_path / "cache", ttl_seconds=3600, enabled=False, suffix="xml.gz"
        ),
        raw_store=OfficialRawStore(tmp_path / "raw", suffix="xml.gz"),
        transport=handler,
    )


def _fed(tmp_path: Path, handler: httpx.MockTransport) -> FederalReserveClient:
    return FederalReserveClient(
        base_url="https://offline.federalreserve.test",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        cache_root=tmp_path / "cache",
        raw_root=tmp_path / "raw",
        cache_ttl_seconds=3600,
        cache_enabled=False,
        transport=handler,
    )


async def test_dol_operational_degradation_does_not_downgrade_reviewed_mapping(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"<notice>database unavailable</notice>")
    )
    async with _dol(tmp_path, transport) as client:
        summary = await DolPipelineService(postgres_session, settings, client).validate_mappings(
            PipelineSelection(indicator_id="US_INITIAL_CLAIMS")
        )

    mapping = postgres_session.scalar(
        select(IndicatorSourceMapping).where(
            IndicatorSourceMapping.source == "dol",
            IndicatorSourceMapping.indicator_id == "US_INITIAL_CLAIMS",
        )
    )
    assert mapping is not None
    assert mapping.mapping_status == "valid"
    assert summary.errors[0].error_type == "source_operational_degradation"
    issue = postgres_session.scalar(
        select(DataQualityEvent).where(
            DataQualityEvent.indicator_id == "US_INITIAL_CLAIMS",
            DataQualityEvent.event_type == "source_operational_degradation",
        )
    )
    assert issue is not None


async def test_dol_unknown_xml_identity_is_a_mapping_error_not_an_outage(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"<report><unknown /></report>")
    )
    async with _dol(tmp_path, transport) as client:
        summary = await DolPipelineService(postgres_session, settings, client).validate_mappings(
            PipelineSelection(indicator_id="US_INITIAL_CLAIMS")
        )

    assert summary.errors[0].error_type == "DolIdentityError"
    assert "unexpected_report_identity" in summary.errors[0].message


async def test_dol_backfill_requests_exactly_one_calendar_year_at_a_time(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    seed_source_mappings(postgres_session, load_source_mapping_catalog_for("dol"))
    mapping = postgres_session.scalar(
        select(IndicatorSourceMapping).where(
            IndicatorSourceMapping.source == "dol",
            IndicatorSourceMapping.indicator_id == "US_INITIAL_CLAIMS",
            IndicatorSourceMapping.enabled.is_(True),
        )
    )
    assert mapping is not None
    mapping.mapping_status = "valid"
    mapping.metadata_checked_at = datetime.now(UTC)
    requested_years: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parameters = parse_qs(request.content.decode())
        start_year = int(parameters["strtdate"][0])
        end_year = int(parameters["enddate"][0])
        assert start_year == end_year
        requested_years.append(start_year)
        xml = (
            f'<r539cyNational rundate="1/11/{start_year}"><week>'
            f"<weekEnded>01/06/{start_year}</weekEnded>"
            "<InitialClaims><SA>200,000</SA></InitialClaims>"
            "</week></r539cyNational>"
        )
        return httpx.Response(200, content=xml.encode(), headers={"content-type": "text/xml"})

    async with _dol(tmp_path, httpx.MockTransport(handler)) as client:
        summary = await DolPipelineService(postgres_session, settings, client).backfill(
            BackfillOptions(
                indicator_id="US_INITIAL_CLAIMS",
                start=date(2024, 1, 1),
                end=date(2025, 12, 31),
                dry_run=True,
            )
        )

    assert requested_years == [2024, 2025]
    assert summary.request_count == 2
    assert summary.success_count == 1
    assert summary.failure_count == 0


async def test_unexpected_g17_base_blocks_pipeline_write_and_emits_quality_event(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("default.htm"):
            return httpx.Response(200, text="<html>Released 2026-08-01; Index 2022=100</html>")
        if request.url.path.endswith("g17tab1.txt"):
            return httpx.Response(200, text="Total index B50001")
        return httpx.Response(200, text='"B50001" 2025 1 2 3 4 5 6 7 8 9 10 11 12')

    async with _fed(tmp_path, httpx.MockTransport(handler)) as client:
        summary = await FederalReservePipelineService(
            postgres_session, settings, client
        ).validate_mappings(PipelineSelection(indicator_id="US_INDUSTRIAL_PRODUCTION"))

    assert summary.failure_count == 1
    assert "unexpected_base_year" in summary.errors[0].message
    assert (
        postgres_session.scalar(
            select(RawObservation).where(
                RawObservation.source == "federal_reserve",
                RawObservation.indicator_id == "US_INDUSTRIAL_PRODUCTION",
            )
        )
        is None
    )
    issue = postgres_session.scalar(
        select(DataQualityEvent).where(
            DataQualityEvent.indicator_id == "US_INDUSTRIAL_PRODUCTION",
            DataQualityEvent.event_type == "unexpected_base_year",
        )
    )
    assert issue is not None
