"""Stage 2 indicator and observation API integration tests."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session

from app.database.models import RawObservation
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.main import create_app
from app.settings import Settings

pytestmark = pytest.mark.integration


def test_indicator_list_observation_pagination_and_key_redaction(
    postgres_engine: Engine,
) -> None:
    with Session(postgres_engine) as session:
        seed_indicator_catalog(session, load_indicator_catalog())
        session.execute(delete(RawObservation).where(RawObservation.indicator_id == "US_CPI"))
        session.add_all(
            [
                RawObservation(
                    indicator_id="US_CPI",
                    observation_date=date(2025, month, 1),
                    value=Decimal(str(300 + month)),
                    source="fred",
                    retrieved_at=datetime.now(UTC),
                    raw_payload_hash=f"{month:064d}",
                )
                for month in (1, 2, 3)
            ]
        )
        session.commit()

    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=postgres_engine.url.render_as_string(hide_password=False),
        fred_api_key=SecretStr("offline-test-credential"),
    )
    with TestClient(create_app(settings)) as client:
        indicators = client.get("/api/v1/indicators", params={"limit": 10})
        observations = client.get(
            "/api/v1/indicators/US_CPI/observations", params={"limit": 2, "offset": 1}
        )
        status = client.get("/api/v1/data-sources/fred/status")

    assert indicators.status_code == 200
    assert len(indicators.json()) == 10
    assert observations.status_code == 200
    assert [row["date"] for row in observations.json()] == ["2025-02-01", "2025-03-01"]
    assert status.status_code == 200
    assert status.json()["api_key_configured"] is True
    assert "offline-test-credential" not in status.text
