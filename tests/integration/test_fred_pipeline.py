"""PostgreSQL integration tests for idempotency and revision retention."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_pipeline.schemas import BackfillOptions
from app.data_pipeline.service import FredPipelineService
from app.data_sources.fred.cache import FredFileCache
from app.data_sources.fred.client import FredClient
from app.data_sources.fred.raw_store import FredRawStore
from app.database.models import DataQualityEvent, ObservationVintage, RawObservation
from app.indicators.config import load_indicator_catalog
from app.indicators.repository import get_indicator
from app.indicators.service import seed_indicator_catalog
from app.settings import Settings

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_backfill_is_idempotent_and_preserves_revision(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    indicator = get_indicator(postgres_session, "US_REAL_GDP")
    assert indicator is not None
    indicator.metadata_status = "valid"
    values = iter(["100.0", "100.0", "101.5"])

    def handler(_: httpx.Request) -> httpx.Response:
        value = next(values)
        return httpx.Response(
            200,
            json={
                "observations": [
                    {
                        "realtime_start": "2025-01-15",
                        "realtime_end": "2025-01-15",
                        "date": "2024-10-01",
                        "value": value,
                    }
                ]
            },
        )

    client = FredClient(
        api_key=SecretStr("offline-test-credential"),
        base_url="https://fred.invalid/fred",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        cache=FredFileCache(tmp_path / "cache", ttl_seconds=0, enabled=False),
        raw_store=FredRawStore(tmp_path / "raw"),
        transport=httpx.MockTransport(handler),
    )
    options = BackfillOptions(
        indicator_id="US_REAL_GDP", start=date(2024, 1, 1), end=date(2025, 1, 1)
    )
    async with client:
        service = FredPipelineService(postgres_session, settings, client)
        first = await service.backfill(options)
        second = await service.backfill(options)
        third = await service.backfill(options)

    assert first.inserted_count == 1
    assert second.inserted_count == second.revised_count == 0
    assert third.revised_count == 1
    current = postgres_session.scalar(
        select(RawObservation).where(RawObservation.indicator_id == "US_REAL_GDP")
    )
    assert current is not None and current.value == Decimal("101.5")
    assert postgres_session.scalar(select(func.count()).select_from(ObservationVintage)) == 1


@pytest.mark.asyncio
async def test_one_source_failure_does_not_rollback_other_indicator(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    for indicator_id in ("US_REAL_GDP", "US_CPI"):
        indicator = get_indicator(postgres_session, indicator_id)
        assert indicator is not None
        indicator.metadata_status = "valid"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["series_id"] == "GDPC1":
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={
                "observations": [
                    {
                        "realtime_start": "2025-02-15",
                        "realtime_end": "2025-02-15",
                        "date": "2025-01-01",
                        "value": "319.1",
                    }
                ]
            },
        )

    client = FredClient(
        api_key=SecretStr("offline-test-credential"),
        base_url="https://fred.invalid/fred",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        cache=FredFileCache(tmp_path / "cache", ttl_seconds=0, enabled=False),
        raw_store=FredRawStore(tmp_path / "raw"),
        transport=httpx.MockTransport(handler),
    )
    async with client:
        summary = await FredPipelineService(postgres_session, settings, client).backfill(
            BackfillOptions(start=date(2025, 1, 1), end=date(2025, 1, 1))
        )

    assert summary.status == "partial"
    assert summary.success_count == 1
    assert summary.failure_count == 1
    assert postgres_session.scalar(
        select(RawObservation.id).where(RawObservation.indicator_id == "US_CPI")
    )
    assert postgres_session.scalar(
        select(DataQualityEvent.id).where(
            DataQualityEvent.indicator_id == "US_REAL_GDP",
            DataQualityEvent.event_type == "source_unavailable",
        )
    )
