"""PostgreSQL tests for strict BEA writes, isolation, batching, and revisions."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_pipeline.bea_service import BeaPipelineService
from app.data_pipeline.schemas import BeaBackfillOptions
from app.data_quality.checks import official_source_issue
from app.data_quality.repository import persist_quality_issues
from app.data_sources.bea.client import BeaClient
from app.data_sources.official.storage import OfficialFileCache, OfficialRawStore
from app.database.models import DataQualityEvent, ObservationVintage, RawObservation
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.settings import Settings


def _row(
    *,
    table: str,
    series: str,
    line: str,
    description: str,
    value: str,
    period: str = "2025M01",
    cl_unit: str = "Billions of chained 2017 dollars",
    metric: str = "Billions of chained 2017 dollars",
    multiplier: str = "9",
) -> dict[str, str]:
    return {
        "TableName": table,
        "SeriesCode": series,
        "LineNumber": line,
        "LineDescription": description,
        "TimePeriod": period,
        "METRIC_NAME": metric,
        "CL_UNIT": cl_unit,
        "UNIT_MULT": multiplier,
        "DataValue": value,
        "NoteRef": "1",
    }


def _client(tmp_path: Path, handler: httpx.MockTransport) -> BeaClient:
    return BeaClient(
        api_key=None,
        base_url="https://offline.bea.test/api/data",
        timeout_seconds=1,
        max_retries=0,
        retry_base_seconds=0,
        cache=OfficialFileCache(tmp_path / "cache", ttl_seconds=3600, enabled=False),
        raw_store=OfficialRawStore(tmp_path / "raw"),
        transport=handler,
    )


def _response(rows: list[dict[str, str]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "BEAAPI": {
                "Results": {
                    "Data": rows,
                    "Notes": [{"NoteRef": "1", "NoteText": "Offline fixture"}],
                }
            }
        },
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bea_backfill_is_idempotent_source_isolated_and_auditable(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    postgres_session.add(
        RawObservation(
            indicator_id="US_REAL_GDP",
            observation_date=datetime(2025, 1, 1, tzinfo=UTC).date(),
            value=Decimal("22000"),
            source="fred",
            retrieved_at=datetime.now(UTC),
            raw_payload_hash="f" * 64,
        )
    )
    postgres_session.flush()

    def handler(_request: httpx.Request) -> httpx.Response:
        return _response(
            [
                _row(
                    table="T10106",
                    series="A191RX",
                    line="1",
                    description="Real gross domestic product",
                    value="22,100.5",
                    period="2025Q1",
                )
            ]
        )

    options = BeaBackfillOptions(indicator_id="US_REAL_GDP", start_year=2025, end_year=2025)
    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        service = BeaPipelineService(postgres_session, settings, client)
        first = await service.backfill(options)
        second = await service.backfill(options)

    assert first.inserted_count == 1
    assert second.inserted_count == 0
    assert second.results[0]["unchanged"] == 1
    rows = postgres_session.scalars(
        select(RawObservation)
        .where(RawObservation.indicator_id == "US_REAL_GDP")
        .order_by(RawObservation.source)
    ).all()
    assert [(item.source, item.value) for item in rows] == [
        ("bea", Decimal("22100.5000000000")),
        ("fred", Decimal("22000.0000000000")),
    ]
    assert rows[0].source_details_json["raw_value"] == "22,100.5"
    assert rows[0].source_details_json["cl_unit"] == "Billions of chained 2017 dollars"
    assert rows[0].source_details_json["unit_mult"] == "9"
    assert rows[0].source_details_json["canonical_value"] == "22100.5"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bea_same_source_change_creates_revision(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    values = iter(["22,100.5", "22,101.0"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return _response(
            [
                _row(
                    table="T10106",
                    series="A191RX",
                    line="1",
                    description="Real gross domestic product",
                    value=next(values),
                    period="2025Q1",
                )
            ]
        )

    options = BeaBackfillOptions(indicator_id="US_REAL_GDP", start_year=2025, end_year=2025)
    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        service = BeaPipelineService(postgres_session, settings, client)
        await service.backfill(options)
        revised = await service.backfill(options)

    assert revised.revised_count == 1
    vintage = postgres_session.scalar(
        select(ObservationVintage).where(
            ObservationVintage.indicator_id == "US_REAL_GDP",
            ObservationVintage.source == "bea",
        )
    )
    assert vintage is not None
    assert vintage.previous_value == Decimal("22100.5000000000")
    assert vintage.current_value == Decimal("22101.0000000000")
    assert vintage.revision_number == 1
    assert vintage.source_details_json["raw_value"] == "22,101.0"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_t20804_request_is_shared_but_candidates_validate_independently(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        assert request.url.params["TableName"] == "T20804"
        return _response(
            [
                _row(
                    table="T20804",
                    series="DPCERG",
                    line="1",
                    description="Personal consumption expenditures (PCE)",
                    value="123.4",
                    cl_unit="Level",
                    metric="Fisher Price Index",
                    multiplier="0",
                ),
                _row(
                    table="T20804",
                    series="DPCCRG",
                    line="25",
                    description="PCE excluding food and energy",
                    value="122.8",
                    cl_unit="Level",
                    metric="Fisher Price Index",
                    multiplier="0",
                ),
            ]
        )

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        summary = await BeaPipelineService(postgres_session, settings, client).backfill(
            BeaBackfillOptions(module="inflation", start_year=2025, end_year=2025)
        )
    assert request_count == 1
    assert summary.success_count == 2
    assert summary.failure_count == 0
    assert summary.inserted_count == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_omits_years_before_official_table_availability(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    requested_years: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_years.append(request.url.params["Year"])
        return _response(
            [
                _row(
                    table="T20806",
                    series="DPCERX",
                    line="1",
                    description="Personal consumption expenditures",
                    period="2007M01",
                    value="10,000,000",
                    cl_unit="Level",
                    metric="Chained Dollars",
                    multiplier="6",
                )
            ]
        )

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        summary = await BeaPipelineService(postgres_session, settings, client).backfill(
            BeaBackfillOptions(indicator_id="US_REAL_PCE", start_year=2000, end_year=2009)
        )

    assert requested_years == ["2007,2008,2009"]
    assert summary.success_count == 1
    assert summary.failure_count == 0
    assert summary.inserted_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bea_discovery_and_exact_get_data_retains_reviewed_status(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    persist_quality_issues(
        postgres_session,
        [
            official_source_issue(
                "US_REAL_GDP",
                event_type="invalid_unit_multiplier",
                severity="error",
                source="bea",
                message="Superseded validation finding",
            )
        ],
        run_id=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.params["Method"]
        if method == "GetDatasetList":
            return httpx.Response(
                200,
                json={"BEAAPI": {"Results": {"Dataset": [{"DatasetName": "NIPA"}]}}},
            )
        if method == "GetParameterList":
            return httpx.Response(
                200,
                json={
                    "BEAAPI": {
                        "Results": {
                            "Parameter": [
                                {"ParameterName": name}
                                for name in ("TableName", "Frequency", "Year")
                            ]
                        }
                    }
                },
            )
        if method == "GetParameterValues":
            parameter = request.url.params["ParameterName"]
            values = {
                "TableName": [{"Key": "T10106"}],
                "Frequency": [{"Key": "Q"}],
                "Year": [
                    {
                        "TableName": "T10106",
                        "FirstAnnualYear": "1929",
                        "LastAnnualYear": str(date.today().year),
                        "FirstQuarterlyYear": "1947",
                        "LastQuarterlyYear": str(date.today().year),
                        "FirstMonthlyYear": "0",
                        "LastMonthlyYear": "0",
                    }
                ],
            }[parameter]
            return httpx.Response(200, json={"BEAAPI": {"Results": {"ParamValue": values}}})
        assert method == "GetData"
        return _response(
            [
                _row(
                    table="T10106",
                    series="A191RX",
                    line="1",
                    description="Real gross domestic product",
                    value="22,100.5",
                    period=f"{date.today().year}Q1",
                )
            ]
        )

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        summary = await BeaPipelineService(postgres_session, settings, client).validate_mappings(
            BeaBackfillOptions(
                indicator_id="US_REAL_GDP",
                start_year=2025,
                end_year=2025,
            )
        )
    assert summary.success_count == 1
    assert summary.failure_count == 0
    assert summary.request_count == 6
    assert summary.results[0]["status"] == "valid_with_warning"
    assert summary.results[0]["identity_confirmed"] is True
    superseded = postgres_session.scalar(
        select(DataQualityEvent).where(
            DataQualityEvent.indicator_id == "US_REAL_GDP",
            DataQualityEvent.event_type == "invalid_unit_multiplier",
        )
    )
    assert superseded is not None
    assert superseded.resolved_at is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrong_bea_line_stops_write_without_auto_substitution(
    postgres_session: Session, settings: Settings, tmp_path: Path
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())

    def handler(_request: httpx.Request) -> httpx.Response:
        return _response(
            [
                _row(
                    table="T10106",
                    series="A191RX",
                    line="2",
                    description="Real gross domestic product",
                    value="22,100.5",
                    period="2025Q1",
                )
            ]
        )

    async with _client(tmp_path, httpx.MockTransport(handler)) as client:
        summary = await BeaPipelineService(postgres_session, settings, client).backfill(
            BeaBackfillOptions(indicator_id="US_REAL_GDP", start_year=2025, end_year=2025)
        )
    assert summary.status == "failed"
    assert summary.inserted_count == 0
    assert summary.failure_count == 1
    assert not postgres_session.scalars(
        select(RawObservation).where(
            RawObservation.indicator_id == "US_REAL_GDP",
            RawObservation.source == "bea",
        )
    ).all()
