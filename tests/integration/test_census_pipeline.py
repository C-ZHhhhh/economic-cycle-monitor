"""PostgreSQL tests for Census DIRECT gates, shared requests, and revisions."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_pipeline.census_service import CensusPipelineService
from app.data_pipeline.schemas import CensusBackfillOptions
from app.data_sources.census.client import CensusClient
from app.data_sources.official.storage import OfficialFileCache, OfficialRawStore
from app.database.models import ObservationVintage, RawObservation
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _client(tmp_path: Path, handler: httpx.MockTransport) -> CensusClient:
    return CensusClient(
        api_key=None,
        base_url="https://offline.census.test/data",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        cache=OfficialFileCache(tmp_path / "cache", ttl_seconds=3600, enabled=False),
        raw_store=OfficialRawStore(tmp_path / "raw"),
        transport=handler,
    )


def _table(values: dict[str, str]) -> list[list[str]]:
    header = [
        "cell_value",
        "data_type_code",
        "time_slot_id",
        "time_slot_date",
        "time_slot_name",
        "error_data",
        "category_code",
        "seasonally_adj",
        "geo_level_code",
        "program_code",
        "time",
    ]
    rows = [
        [value, "NO", "01", "2025-01", "January", "no", category, "yes", "US", "M3", "2025"]
        for category, value in values.items()
    ]
    return [header, *rows]


def _review(service: CensusPipelineService, indicator_ids: set[str]) -> None:
    definitions = []
    for definition in service.catalog.mappings:
        if definition.indicator_id not in indicator_ids:
            definitions.append(definition)
            continue
        parameters = {
            **definition.source_parameters,
            "seasonally_adj": "yes",
            "geo_level_code": "US",
        }
        definitions.append(
            definition.model_copy(
                update={"source_parameters": parameters, "mapping_status": "valid"}
            )
        )
    service.catalog = service.catalog.model_copy(update={"mappings": definitions})
    service.definitions = {item.indicator_id: item for item in definitions}


async def test_m3_request_is_shared_idempotent_and_source_isolated(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    postgres_session.add(
        RawObservation(
            indicator_id="US_DURABLE_GOODS_ORDERS",
            observation_date=datetime(2025, 1, 1, tzinfo=UTC).date(),
            value=Decimal("320000"),
            source="fred",
            retrieved_at=datetime.now(UTC),
            raw_payload_hash="f" * 64,
        )
    )
    postgres_session.flush()
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=_table({"MDM": "321000", "NXA": "80000"}))

    options = CensusBackfillOptions(
        module="business_activity", start=date(2025, 1, 1), end=date(2025, 1, 1)
    )
    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        service = CensusPipelineService(postgres_session, settings, client)
        _review(
            service,
            {"US_DURABLE_GOODS_ORDERS", "US_CORE_CAPITAL_GOODS_ORDERS"},
        )
        first = await service.backfill(options)
        second = await service.backfill(options)

    assert requests == 2
    assert first.success_count == 2
    assert first.inserted_count == 2
    assert second.inserted_count == 0
    assert sum(item["unchanged"] for item in second.results) == 2
    rows = postgres_session.scalars(
        select(RawObservation).where(RawObservation.indicator_id == "US_DURABLE_GOODS_ORDERS")
    ).all()
    assert {item.source for item in rows} == {"census", "fred"}
    census = next(item for item in rows if item.source == "census")
    assert census.source_details_json["raw_cell_value"] == "321000"
    assert census.source_details_json["source_identity"]["category_code"] == "MDM"
    assert len(census.source_details_json["source_identity_hash"]) == 64


async def test_supporting_component_never_writes_target_observations(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_table({"44X72": "700000"}))

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        service = CensusPipelineService(postgres_session, settings, client)
        _review(service, {"US_REAL_RETAIL_SALES"})
        summary = await service.backfill(
            CensusBackfillOptions(
                indicator_id="US_REAL_RETAIL_SALES",
                start=date(2025, 1, 1),
                end=date(2025, 1, 1),
            )
        )

    assert calls == 0
    assert summary.indicator_count == 0
    assert (
        postgres_session.scalar(
            select(RawObservation).where(
                RawObservation.indicator_id == "US_REAL_RETAIL_SALES",
                RawObservation.source == "census",
            )
        )
        is None
    )


async def test_same_source_value_change_creates_census_revision(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    values = iter(["321000", "321001"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_table({"MDM": next(values)}))

    options = CensusBackfillOptions(
        indicator_id="US_DURABLE_GOODS_ORDERS",
        start=date(2025, 1, 1),
        end=date(2025, 1, 1),
    )
    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        service = CensusPipelineService(postgres_session, settings, client)
        _review(service, {"US_DURABLE_GOODS_ORDERS"})
        await service.backfill(options)
        revised = await service.backfill(options)

    assert revised.revised_count == 1
    vintage = postgres_session.scalar(
        select(ObservationVintage).where(
            ObservationVintage.indicator_id == "US_DURABLE_GOODS_ORDERS",
            ObservationVintage.source == "census",
        )
    )
    assert vintage is not None
    assert vintage.previous_value == Decimal("321000.0000000000")
    assert vintage.current_value == Decimal("321001.0000000000")
    assert vintage.revision_number == 1
