"""Live PostgreSQL foreign-key and uniqueness tests."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import IndicatorRegistry, RawObservation

pytestmark = pytest.mark.integration


def test_raw_observation_unique_constraint(postgres_session: Session) -> None:
    indicator = IndicatorRegistry(
        indicator_id="test.unique.series",
        name="Test series",
        description="Small integration-test record",
        module="test",
        owner_agent="us_macro_cycle_agent",
        primary_source="test",
        observation_frequency="monthly",
        release_frequency="monthly",
        unit="index",
        seasonal_adjustment="none",
        direction="procyclical",
        weight=Decimal("0"),
        max_staleness_days=30,
        revision_policy="latest",
        config_version="0.1.0",
    )
    postgres_session.add(indicator)
    postgres_session.flush()
    common = {
        "indicator_id": indicator.indicator_id,
        "observation_date": date(2026, 1, 1),
        "value": Decimal("1"),
        "source": "test",
        "retrieved_at": datetime.now(UTC),
        "raw_payload_hash": "a" * 64,
    }
    postgres_session.add_all([RawObservation(**common), RawObservation(**common)])
    with pytest.raises(IntegrityError):
        postgres_session.flush()
