"""FastAPI database readiness tests."""

import pytest
from fastapi.testclient import TestClient

from app.api.routes import health as health_routes
from app.main import create_app
from app.settings import Settings


def test_ready_when_database_responds(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_routes, "check_database", lambda _engine: None)
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


def test_ready_returns_safe_503(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_engine: object) -> None:
        raise OSError("postgresql+psycopg://user:password@host/private")

    monkeypatch.setattr(health_routes, "check_database", unavailable)
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    payload = response.json()["error"]
    assert payload["code"] == "not_ready"
    assert payload["message"] == "Database is unavailable"
    assert "password" not in response.text
