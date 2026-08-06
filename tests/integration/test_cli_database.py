"""Live PostgreSQL CLI persistence test."""

from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app import cli
from app.database.models import AgentReport, AgentRun

pytestmark = pytest.mark.integration
runner = CliRunner()


def test_cli_create_mock_run_against_postgres(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", str(postgres_engine.url))
    result = runner.invoke(cli.app, ["create-mock-run"])
    assert result.exit_code == 0, result.output
    run_id = UUID(result.output.strip().rsplit(" ", maxsplit=1)[-1])
    with Session(postgres_engine) as session:
        assert session.get(AgentRun, run_id) is not None
        assert session.scalar(select(AgentReport).where(AgentReport.run_id == run_id)) is not None
