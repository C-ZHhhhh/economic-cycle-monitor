"""PostgreSQL gates for public-source metadata and operational failures."""

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_pipeline.dol_service import DolPipelineService
from app.data_pipeline.fed_service import FederalReservePipelineService
from app.data_pipeline.schemas import PipelineSelection
from app.data_sources.dol.client import DolClient
from app.data_sources.federal_reserve.client import FederalReserveClient
from app.data_sources.official.storage import OfficialFileCache, OfficialRawStore
from app.database.models import DataQualityEvent, IndicatorSourceMapping, RawObservation
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.settings import Settings

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


async def test_dol_operational_degradation_keeps_mapping_pending_and_emits_quality_event(
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
    assert mapping.mapping_status == "pending_review"
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
    assert "pending_xml_identity" in summary.errors[0].message


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
