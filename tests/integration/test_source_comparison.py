"""PostgreSQL tests for BLS/FRED comparison classifications."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.data_pipeline.schemas import SourceComparisonOptions
from app.data_pipeline.source_comparison import SourceComparisonService
from app.database.models import RawObservation
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog


def _observation(source: str, when: date, value: str) -> RawObservation:
    return RawObservation(
        indicator_id="US_UNEMPLOYMENT_RATE",
        observation_date=when,
        value=Decimal(value),
        source=source,
        retrieved_at=datetime.now(UTC),
        raw_payload_hash=("b" if source == "bls" else "f") * 64,
    )


@pytest.mark.integration
def test_comparison_classifies_match_rounding_lag_missing_and_conflict(
    postgres_session: Session,
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    old = date.today() - timedelta(days=120)
    dates = [old + timedelta(days=index) for index in range(5)]
    postgres_session.add_all(
        [
            _observation("bls", dates[0], "4.0"),
            _observation("fred", dates[0], "4.0"),
            _observation("bls", dates[1], "4.0"),
            _observation("fred", dates[1], "4.05"),
            _observation("bls", dates[2], "4.0"),
            _observation("fred", dates[2], "5.0"),
            _observation("fred", dates[3], "4.0"),
            _observation("bls", dates[4], "4.0"),
            _observation("bls", date.today(), "4.0"),
        ]
    )
    postgres_session.flush()
    summary = SourceComparisonService(postgres_session).compare(
        SourceComparisonOptions(indicator_id="US_UNEMPLOYMENT_RATE")
    )
    assert summary.status == "succeeded"
    assert summary.results[0]["counts"] == {
        "distribution_lag": 1,
        "match": 1,
        "match_with_rounding": 1,
        "missing_in_bls": 1,
        "missing_in_fred": 1,
        "source_value_conflict": 1,
    }
    assert summary.quality_event_count == 4


@pytest.mark.integration
@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("unit", "persons", "normalization_mismatch"),
        ("seasonal_adjustment", "not_seasonally_adjusted", "seasonal_adjustment_mismatch"),
    ],
)
def test_comparison_blocks_incompatible_semantics(
    postgres_session: Session, field: str, value: str, expected: str
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    indicator = next(
        item
        for item in load_indicator_catalog().indicators
        if item.indicator_id == "US_UNEMPLOYMENT_RATE"
    )
    from app.indicators.repository import get_indicator

    record = get_indicator(postgres_session, indicator.indicator_id)
    assert record is not None
    setattr(record, field, value)
    postgres_session.add_all(
        [
            _observation("bls", date(2025, 1, 1), "4.0"),
            _observation("fred", date(2025, 1, 1), "4.0"),
        ]
    )
    postgres_session.flush()
    summary = SourceComparisonService(postgres_session).compare(
        SourceComparisonOptions(indicator_id="US_UNEMPLOYMENT_RATE")
    )
    assert summary.results[0]["counts"] == {expected: 1}
    assert summary.quality_event_count == 1
