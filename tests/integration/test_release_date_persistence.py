"""Round-trip release-date tests using existing Stage 2 persistence columns."""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_pipeline.repository import persist_official_observations
from app.data_sources.official.schemas import OfficialObservation
from app.database.models import ObservationVintage, RawObservation
from app.indicators.config import load_indicator_catalog
from app.indicators.repository import get_indicator
from app.indicators.service import seed_indicator_catalog


def test_release_date_round_trips_for_current_and_revision(
    postgres_session: Session,
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    indicator = get_indicator(postgres_session, "US_INITIAL_CLAIMS")
    assert indicator is not None
    retrieved = datetime(2026, 8, 7, tzinfo=UTC)
    first = OfficialObservation(
        date=date(2026, 8, 1),
        value=Decimal("100"),
        source_period="2026-08-01",
        release_date=date(2026, 8, 6),
        release_date_status="provided",
    )
    persist_official_observations(
        postgres_session,
        indicator=indicator,
        source="dol",
        source_series_id="test",
        observations=[first],
        payload_hash="a" * 64,
        retrieved_at=retrieved,
    )
    current = postgres_session.scalar(
        select(RawObservation).where(
            RawObservation.indicator_id == indicator.indicator_id,
            RawObservation.source == "dol",
        )
    )
    assert current is not None
    assert current.source_details_json["release_date"] == "2026-08-06"
    assert current.source_details_json["release_date_status"] == "provided"

    revised = first.model_copy(update={"value": Decimal("101"), "release_date": date(2026, 8, 13)})
    persist_official_observations(
        postgres_session,
        indicator=indicator,
        source="dol",
        source_series_id="test",
        observations=[revised],
        payload_hash="b" * 64,
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    vintage = postgres_session.scalar(
        select(ObservationVintage).where(
            ObservationVintage.indicator_id == indicator.indicator_id,
            ObservationVintage.source == "dol",
        )
    )
    assert vintage is not None
    assert vintage.release_date == date(2026, 8, 13)


def test_absent_release_date_round_trips_with_not_provided_status(
    postgres_session: Session,
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    indicator = get_indicator(postgres_session, "US_INDUSTRIAL_PRODUCTION")
    assert indicator is not None
    persist_official_observations(
        postgres_session,
        indicator=indicator,
        source="federal_reserve",
        source_series_id="IP.B50001.S",
        observations=[
            OfficialObservation(
                date=date(2026, 7, 1),
                value=Decimal("100"),
                source_period="2026-07",
            )
        ],
        payload_hash="c" * 64,
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    current = postgres_session.scalar(
        select(RawObservation).where(
            RawObservation.indicator_id == indicator.indicator_id,
            RawObservation.source == "federal_reserve",
        )
    )
    assert current is not None
    assert current.source_details_json["release_date"] is None
    assert current.source_details_json["release_date_status"] == "not_provided"
