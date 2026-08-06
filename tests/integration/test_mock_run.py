"""Live PostgreSQL mock-run persistence and API tests."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from agents.macro.service import create_mock_run
from app.database.models import AgentReport, AgentRun
from app.main import create_app
from app.settings import Settings

pytestmark = pytest.mark.integration


def test_create_mock_run_persists_run_and_report(postgres_session: Session) -> None:
    settings = Settings(_env_file=None, app_env="test", llm_mode="mock")
    result = asyncio.run(create_mock_run(postgres_session, settings))
    postgres_session.flush()
    assert result.data_quality_status == "not_run"
    assert result.current_regime is None
    assert postgres_session.scalar(select(func.count()).select_from(AgentRun)) == 1
    assert postgres_session.scalar(select(func.count()).select_from(AgentReport)) == 1


def test_runs_api_returns_persisted_mock(postgres_engine: Engine) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=str(postgres_engine.url),
        llm_mode="mock",
    )
    with Session(postgres_engine) as session:
        result = asyncio.run(create_mock_run(session, settings))
        session.commit()

    with TestClient(create_app(settings)) as client:
        response = client.get(f"/api/v1/runs/{result.run_id}")
    assert response.status_code == 200
    assert response.json()["run"]["run_id"] == str(result.run_id)
    assert response.json()["reports"][0]["result_json"]["agent"] == "us_macro_cycle_agent"
