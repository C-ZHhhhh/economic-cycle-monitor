"""Offline completeness and lookup tests for DLCR-003 change indexes."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.data_sources.fred.schemas import FredPayload, FredVintageDatePage
from app.source_registry.materialization import load_pit_materialization_catalog
from app.source_registry.pit_index import (
    ChangeIndexDiscovery,
    ChangeIndexIncomplete,
    ChangeIndexMalformed,
    affected_intervals,
    refresh_change_index,
    validate_change_index_pages,
)


def _payload(response_hash: str) -> FredPayload:
    now = datetime(2026, 8, 31, tzinfo=UTC)
    return FredPayload(
        endpoint="series/vintagedates",
        series_id="TEST",
        sanitized_parameters={},
        body={},
        started_at=now,
        finished_at=now,
        status_code=200,
        attempt_count=1,
        response_hash=response_hash,
        response_path=Path("safe.json"),
        from_cache=False,
    )


def _validate(pages: list[FredVintageDatePage]) -> ChangeIndexDiscovery:
    return validate_change_index_pages(
        [(item, _payload(str(index) * 64)) for index, item in enumerate(pages, start=1)],
        coverage_start=date(1900, 1, 1),
        coverage_end=date(2026, 8, 30),
        source_registry_hash="r" * 64,
        materialization=load_pit_materialization_catalog(),
        indicator_id="US_TEST",
        provider_series_id="TEST",
    )


def test_multiple_pages_close_exactly() -> None:
    result = _validate(
        [
            FredVintageDatePage(
                count=3,
                offset=0,
                limit=10_000,
                vintage_dates=[date(2020, 1, 1), date(2020, 2, 1)],
            ),
            FredVintageDatePage(
                count=3,
                offset=2,
                limit=10_000,
                vintage_dates=[date(2020, 3, 1)],
            ),
        ]
    )
    assert result.change_dates == (
        date(2020, 1, 1),
        date(2020, 2, 1),
        date(2020, 3, 1),
    )
    assert result.provider_count == 3


def test_page_limit_does_not_truncate_provider_count_above_ten_thousand() -> None:
    start = date(1900, 1, 1)
    dates = [start + timedelta(days=offset) for offset in range(10_001)]
    result = _validate(
        [
            FredVintageDatePage(
                count=10_001,
                offset=0,
                limit=10_000,
                vintage_dates=dates[:10_000],
            ),
            FredVintageDatePage(
                count=10_001,
                offset=10_000,
                limit=10_000,
                vintage_dates=dates[10_000:],
            ),
        ]
    )
    assert result.provider_count == 10_001
    assert len(result.change_dates) == 10_001
    assert result.change_dates[-1] == dates[-1]


@pytest.mark.parametrize(
    ("pages", "error"),
    [
        (
            [
                FredVintageDatePage(
                    count=2, offset=0, limit=10_000, vintage_dates=[date(2020, 1, 1)]
                )
            ],
            ChangeIndexIncomplete,
        ),
        (
            [
                FredVintageDatePage(
                    count=2, offset=0, limit=10_000, vintage_dates=[date(2020, 1, 1)]
                ),
                FredVintageDatePage(
                    count=3, offset=1, limit=10_000, vintage_dates=[date(2020, 2, 1)]
                ),
            ],
            ChangeIndexMalformed,
        ),
        (
            [
                FredVintageDatePage(
                    count=2,
                    offset=0,
                    limit=10_000,
                    vintage_dates=[date(2020, 1, 1), date(2020, 1, 1)],
                )
            ],
            ChangeIndexMalformed,
        ),
        (
            [
                FredVintageDatePage(
                    count=2,
                    offset=0,
                    limit=10_000,
                    vintage_dates=[date(2020, 2, 1), date(2020, 1, 1)],
                )
            ],
            ChangeIndexMalformed,
        ),
    ],
)
def test_invalid_pagination_fails_closed(
    pages: list[FredVintageDatePage], error: type[Exception]
) -> None:
    with pytest.raises(error):
        _validate(pages)


def test_precise_invalidation_intervals() -> None:
    assert affected_intervals(
        (date(2020, 1, 1), date(2020, 3, 1)),
        (date(2020, 1, 1), date(2020, 2, 1), date(2020, 3, 1)),
    ) == [(date(2020, 2, 1), date(2020, 3, 1))]


@pytest.mark.asyncio
async def test_incremental_refresh_creates_combined_immutable_generation() -> None:
    materialization = load_pit_materialization_catalog()
    from app.database.models import PitChangeIndexGeneration

    prior = PitChangeIndexGeneration(
        indicator_id="US_TEST",
        provider="ALFRED_VINTAGE",
        provider_series_id="TEST",
        status="COMPLETE",
        index_coverage_start=date(2000, 1, 1),
        index_coverage_end=date(2020, 2, 29),
        index_complete_through=date(2020, 2, 29),
        provider_count=2,
        retrieved_count=2,
        page_count=1,
        change_dates_json=["2020-01-01", "2020-02-01"],
        pagination_evidence_json=[
            {
                "count": 2,
                "offset": 0,
                "limit": 10_000,
                "retrieved": 2,
                "response_hash": "a" * 64,
                "from_cache": False,
            }
        ],
        generation_hash="b" * 64,
        source_registry_hash="r" * 64,
        pit_materialization_spec_version="pit-materialization-v2",
        pit_materialization_spec_hash=materialization.specification_hash,
        discovered_at=datetime(2026, 8, 31, tzinfo=UTC),
    )
    client = AsyncMock()
    client.get_vintage_date_page.return_value = (
        FredVintageDatePage(
            count=1,
            offset=0,
            limit=10_000,
            vintage_dates=[date(2020, 3, 1)],
        ),
        _payload("c" * 64),
    )
    discovery, payloads = await refresh_change_index(
        client,
        prior=prior,
        coverage_end=date(2020, 3, 31),
        source_registry_hash="r" * 64,
        materialization=materialization,
    )
    assert prior.change_dates_json == ["2020-01-01", "2020-02-01"]
    assert discovery.change_dates == (
        date(2020, 1, 1),
        date(2020, 2, 1),
        date(2020, 3, 1),
    )
    assert discovery.complete_through == date(2020, 3, 31)
    assert discovery.generation_hash != prior.generation_hash
    assert len(payloads) == 1
    client.get_vintage_date_page.assert_awaited_once_with(
        "TEST",
        realtime_start=date(2020, 3, 1),
        realtime_end=date(2020, 3, 31),
        limit=10_000,
        offset=0,
    )
