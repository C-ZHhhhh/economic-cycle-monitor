"""PostgreSQL tests for BLS/FRED comparison classifications."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.data_pipeline.schemas import SourceComparisonOptions
from app.data_pipeline.source_comparison import SourceComparisonService
from app.database.models import IndicatorSourceMapping, RawObservation
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.source_mappings.config import load_source_mapping_catalog_for
from app.source_mappings.repository import seed_source_mappings


def _observation(source: str, when: date, value: str) -> RawObservation:
    return RawObservation(
        indicator_id="US_UNEMPLOYMENT_RATE",
        observation_date=when,
        value=Decimal(value),
        source=source,
        retrieved_at=datetime.now(UTC),
        raw_payload_hash=source[0] * 64,
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


@pytest.mark.integration
def test_bea_comparison_uses_canonical_values_and_dynamic_missing_status(
    postgres_session: Session,
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    postgres_session.add_all(
        [
            RawObservation(
                indicator_id="US_REAL_GDP",
                observation_date=date(2025, 1, 1),
                value=Decimal("22100.5"),
                source="bea",
                retrieved_at=datetime.now(UTC),
                raw_payload_hash="b" * 64,
                source_details_json={
                    "raw_value": "22,100.5",
                    "cl_unit": "Billions of chained 2017 dollars",
                    "unit_mult": "9",
                    "canonical_value": "22100.5",
                },
            ),
            RawObservation(
                indicator_id="US_REAL_GDP",
                observation_date=date(2025, 1, 1),
                value=Decimal("22100.5"),
                source="fred",
                retrieved_at=datetime.now(UTC),
                raw_payload_hash="f" * 64,
            ),
            RawObservation(
                indicator_id="US_REAL_GDP",
                observation_date=date(2024, 10, 1),
                value=Decimal("22000"),
                source="fred",
                retrieved_at=datetime.now(UTC),
                raw_payload_hash="f" * 64,
            ),
        ]
    )
    postgres_session.flush()

    summary = SourceComparisonService(postgres_session).compare(
        SourceComparisonOptions(
            indicator_id="US_REAL_GDP", primary_source="bea", secondary_source="fred"
        )
    )

    assert summary.results[0]["counts"] == {"match": 1, "missing_in_bea": 1}


@pytest.mark.integration
def test_bea_comparison_rejects_missing_unit_multiplier_audit_evidence(
    postgres_session: Session,
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    postgres_session.add_all(
        [
            RawObservation(
                indicator_id="US_REAL_GDP",
                observation_date=date(2025, 1, 1),
                value=Decimal("22100.5"),
                source="bea",
                retrieved_at=datetime.now(UTC),
                raw_payload_hash="b" * 64,
            ),
            RawObservation(
                indicator_id="US_REAL_GDP",
                observation_date=date(2025, 1, 1),
                value=Decimal("22100.5"),
                source="fred",
                retrieved_at=datetime.now(UTC),
                raw_payload_hash="f" * 64,
            ),
        ]
    )
    postgres_session.flush()

    summary = SourceComparisonService(postgres_session).compare(
        SourceComparisonOptions(
            indicator_id="US_REAL_GDP", primary_source="bea", secondary_source="fred"
        )
    )

    assert summary.results[0]["counts"] == {"normalization_mismatch": 1}


@pytest.mark.integration
def test_census_supporting_component_is_excluded_from_direct_comparison(
    postgres_session: Session,
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    seed_source_mappings(postgres_session, load_source_mapping_catalog_for("census"))
    census_mapping = next(
        item
        for item in postgres_session.query(IndicatorSourceMapping).filter_by(source="census").all()
        if item.indicator_id == "US_REAL_RETAIL_SALES"
    )
    census_mapping.mapping_status = "valid"
    census_mapping.metadata_checked_at = datetime.now(UTC)
    postgres_session.flush()

    summary = SourceComparisonService(postgres_session).compare(
        SourceComparisonOptions(
            indicator_id="US_REAL_RETAIL_SALES",
            primary_source="census",
            secondary_source="fred",
        )
    )

    assert summary.indicator_count == 0
    assert summary.results == []
