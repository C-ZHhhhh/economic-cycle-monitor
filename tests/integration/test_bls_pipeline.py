"""PostgreSQL integration tests for BLS isolation, idempotency, and revisions."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_pipeline.bls_service import BlsPipelineService
from app.data_pipeline.schemas import BlsBackfillOptions
from app.data_sources.bls.cache import BlsFileCache
from app.data_sources.bls.client import BlsClient
from app.data_sources.bls.raw_store import BlsRawStore
from app.database.models import ObservationVintage, RawObservation
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.settings import Settings


def _client(tmp_path: Path, values: list[str]) -> BlsClient:
    call = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call
        body = json.loads(request.content)
        value = values[min(call, len(values) - 1)]
        call += 1
        return httpx.Response(
            200,
            json={
                "status": "REQUEST_SUCCEEDED",
                "message": [],
                "Results": {
                    "series": [
                        {
                            "seriesID": series_id,
                            "data": [
                                {
                                    "year": "2025",
                                    "period": "M01",
                                    "periodName": "January",
                                    "value": value,
                                    "footnotes": [{"code": "P", "text": "Preliminary"}],
                                }
                            ],
                        }
                        for series_id in body["seriesid"]
                    ]
                },
            },
        )

    return BlsClient(
        api_key=None,
        base_url="https://offline.bls.test/publicAPI/v2",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        max_series_per_request=25,
        max_years_per_request=10,
        cache=BlsFileCache(tmp_path / "cache", ttl_seconds=3600, enabled=False),
        raw_store=BlsRawStore(tmp_path / "raw"),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bls_backfill_is_idempotent_and_does_not_overwrite_fred(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    postgres_session.add(
        RawObservation(
            indicator_id="US_UNEMPLOYMENT_RATE",
            observation_date=datetime(2025, 1, 1, tzinfo=UTC).date(),
            value=Decimal("3.9"),
            source="fred",
            retrieved_at=datetime.now(UTC),
            raw_payload_hash="f" * 64,
        )
    )
    postgres_session.flush()
    options = BlsBackfillOptions(
        indicator_id="US_UNEMPLOYMENT_RATE",
        start_year=2025,
        end_year=2025,
    )
    async with _client(tmp_path, ["4.0", "4.0"]) as client:
        service = BlsPipelineService(postgres_session, settings, client)
        first = await service.backfill(options)
        second = await service.backfill(options)
    assert first.inserted_count == 1
    assert second.inserted_count == 0
    assert second.results[0]["unchanged"] == 1
    rows = postgres_session.scalars(
        select(RawObservation)
        .where(RawObservation.indicator_id == "US_UNEMPLOYMENT_RATE")
        .order_by(RawObservation.source)
    ).all()
    assert [(item.source, item.value) for item in rows] == [
        ("bls", Decimal("4.0000000000")),
        ("fred", Decimal("3.9000000000")),
    ]
    assert rows[0].source_details_json["is_preliminary"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bls_same_source_change_creates_revision_vintage(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    options = BlsBackfillOptions(
        indicator_id="US_UNEMPLOYMENT_RATE",
        start_year=2025,
        end_year=2025,
    )
    async with _client(tmp_path, ["100.0", "101.0"]) as client:
        service = BlsPipelineService(postgres_session, settings, client)
        await service.backfill(options)
        revised = await service.backfill(options)
    assert revised.revised_count == 1
    assert revised.quality_event_count == 1
    current = postgres_session.scalar(
        select(RawObservation).where(
            RawObservation.indicator_id == "US_UNEMPLOYMENT_RATE",
            RawObservation.source == "bls",
        )
    )
    assert current is not None
    assert current.value == Decimal("101.0000000000")
    vintage = postgres_session.scalar(
        select(ObservationVintage).where(
            ObservationVintage.indicator_id == "US_UNEMPLOYMENT_RATE",
            ObservationVintage.source == "bls",
        )
    )
    assert vintage is not None
    assert vintage.previous_value == Decimal("100.0000000000")
    assert vintage.current_value == Decimal("101.0000000000")
    assert vintage.revision_number == 1
    assert (
        postgres_session.scalar(
            select(func.count())
            .select_from(ObservationVintage)
            .where(
                ObservationVintage.indicator_id == "US_UNEMPLOYMENT_RATE",
                ObservationVintage.source == "fred",
            )
        )
        == 0
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_partial_bls_batch_keeps_returned_series_and_flags_omissions(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())

    def handler(request: httpx.Request) -> httpx.Response:
        requested = json.loads(request.content)["seriesid"]
        return httpx.Response(
            200,
            json={
                "status": "REQUEST_SUCCEEDED",
                "message": [],
                "Results": {
                    "series": [
                        {
                            "seriesID": requested[0],
                            "data": [
                                {
                                    "year": "2025",
                                    "period": "M01",
                                    "periodName": "January",
                                    "value": "100",
                                    "footnotes": [{}],
                                }
                            ],
                        }
                    ]
                },
            },
        )

    client = BlsClient(
        api_key=None,
        base_url="https://offline.bls.test/publicAPI/v2",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        max_series_per_request=25,
        max_years_per_request=10,
        cache=BlsFileCache(tmp_path / "cache", ttl_seconds=3600, enabled=False),
        raw_store=BlsRawStore(tmp_path / "raw"),
        transport=httpx.MockTransport(handler),
    )
    async with client:
        summary = await BlsPipelineService(postgres_session, settings, client).backfill(
            BlsBackfillOptions(module="inflation", start_year=2025, end_year=2025)
        )
    assert summary.status == "partial"
    assert summary.success_count == 1
    assert summary.failure_count == 2
    assert summary.inserted_count == 1
    assert summary.quality_event_count == 2
