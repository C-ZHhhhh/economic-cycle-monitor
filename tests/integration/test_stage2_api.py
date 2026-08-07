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
from app.source_mappings.config import (
    load_source_mapping_catalog,
    load_source_mapping_catalog_for,
)
from app.source_mappings.repository import seed_source_mappings

pytestmark = pytest.mark.integration


def test_indicator_list_observation_pagination_and_key_redaction(
    postgres_engine: Engine,
) -> None:
    with Session(postgres_engine) as session:
        seed_indicator_catalog(session, load_indicator_catalog())
        seed_source_mappings(session, load_source_mapping_catalog())
        seed_source_mappings(session, load_source_mapping_catalog_for("bea"))
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
        session.add(
            RawObservation(
                indicator_id="US_CPI",
                observation_date=date(2025, 3, 1),
                value=Decimal("303.1"),
                source="bls",
                retrieved_at=datetime.now(UTC),
                raw_payload_hash="b" * 64,
            )
        )
        session.commit()

    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=postgres_engine.url.render_as_string(hide_password=False),
        fred_api_key=SecretStr("offline-test-credential"),
        bls_api_key=SecretStr("offline-bls-test-credential"),
        bls_enabled=True,
        bea_api_key=SecretStr("offline-bea-test-credential"),
        bea_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        indicators = client.get("/api/v1/indicators", params={"limit": 10})
        observations = client.get(
            "/api/v1/indicators/US_CPI/observations",
            params={"limit": 2, "offset": 1, "source": "fred"},
        )
        bls_observations = client.get(
            "/api/v1/indicators/US_CPI/observations", params={"source": "BLS"}
        )
        status = client.get("/api/v1/data-sources/fred/status")
        bls_status = client.get("/api/v1/data-sources/bls/status")
        bea_status = client.get("/api/v1/data-sources/bea/status")
        mappings = client.get("/api/v1/source-mappings", params={"source": "bls", "limit": 5})
        bea_mappings = client.get("/api/v1/source-mappings", params={"source": "BEA", "limit": 5})
        mapping = client.get("/api/v1/source-mappings/US_CPI")

    assert indicators.status_code == 200
    assert len(indicators.json()) == 10
    assert observations.status_code == 200
    assert [row["date"] for row in observations.json()] == ["2025-02-01", "2025-03-01"]
    assert bls_observations.status_code == 200
    assert [row["source"] for row in bls_observations.json()] == ["bls"]
    assert status.status_code == 200
    assert status.json()["api_key_configured"] is True
    assert "offline-test-credential" not in status.text
    assert bls_status.status_code == 200
    assert bls_status.json()["api_key_configured"] is True
    assert "offline-bls-test-credential" not in bls_status.text
    assert bea_status.status_code == 200
    assert bea_status.json()["api_key_configured"] is True
    assert bea_status.json()["pending_review_count"] == 5
    assert "offline-bea-test-credential" not in bea_status.text
    assert mappings.status_code == 200
    assert len(mappings.json()) == 5
    assert bea_mappings.status_code == 200
    assert len(bea_mappings.json()) == 5
    assert {item["mapping_status"] for item in bea_mappings.json()} == {"pending_review"}
    assert mapping.status_code == 200
    assert mapping.json()[0]["source_series_id"] == "CUSR0000SA0"
