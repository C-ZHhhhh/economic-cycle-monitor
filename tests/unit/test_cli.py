"""Typer command tests that remain offline."""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from agents.macro.schemas import MacroCycleResult
from app import cli

runner = CliRunner()


class DummyEngine:
    """Minimal disposable engine used by CLI unit tests."""

    disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_cli_health(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = DummyEngine()
    monkeypatch.setattr(cli, "create_database_engine", lambda _url: engine)
    monkeypatch.setattr(cli, "check_database", lambda _engine: None)
    result = runner.invoke(cli.app, ["health"])
    assert result.exit_code == 0
    assert "healthy" in result.stdout
    assert engine.disposed


def test_cli_show_config_redacts_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "never-print-this")
    result = runner.invoke(cli.app, ["show-config"])
    assert result.exit_code == 0
    assert "never-print-this" not in result.stdout
    assert '"deepseek_api_key": "***"' in result.stdout


def test_cli_create_mock_run(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = DummyEngine()
    run_id = uuid4()

    @contextmanager
    def fake_session(_engine: DummyEngine) -> Generator[object, None, None]:
        yield object()

    async def fake_create(_session: object, _settings: object) -> MacroCycleResult:
        return MacroCycleResult(run_id=run_id, as_of=datetime.now(UTC))

    monkeypatch.setattr(cli, "create_database_engine", lambda _url: engine)
    monkeypatch.setattr(cli, "db_session", fake_session)
    monkeypatch.setattr(cli, "create_mock_run_service", fake_create)
    result = runner.invoke(cli.app, ["create-mock-run"])
    assert result.exit_code == 0
    assert str(run_id) in result.stdout
    assert engine.disposed
