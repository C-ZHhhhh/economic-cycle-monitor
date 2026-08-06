"""FastAPI liveness and safe configuration tests."""

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings


def test_health_does_not_require_database(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "economic-cycle-monitor",
        "version": "0.1.0",
    }
    assert response.headers["X-Request-ID"]


def test_config_summary_never_contains_secret() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        deepseek_api_key="do-not-return-this",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/system/config-summary")
    assert response.status_code == 200
    assert "do-not-return-this" not in response.text
    assert response.json()["deepseek_api_key"] == "***"


def test_macro_schema_endpoint(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/macro/schema")
    assert response.status_code == 200
    assert response.json()["title"] == "MacroCycleResult"
    assert "run_id" in response.json()["required"]
