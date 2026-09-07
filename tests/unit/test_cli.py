"""Typer command tests that remain offline."""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
import typer
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


def test_every_stage_two_command_has_help() -> None:
    commands = [
        "seed-indicators",
        "list-indicators",
        "show-indicator",
        "validate-fred-metadata",
        "backfill-fred",
        "sync-fred",
        "fetch-fred-vintage",
        "check-data-quality",
        "show-sync-status",
        "clear-fred-cache",
        "validate-bls-mappings",
        "backfill-bls",
        "sync-bls",
        "compare-sources",
        "show-source-status",
        "clear-bls-cache",
        "validate-bea-mappings",
        "backfill-bea",
        "sync-bea",
        "clear-bea-cache",
        "validate-census-mappings",
        "backfill-census",
        "sync-census",
        "clear-census-cache",
        "validate-federal-reserve-mappings",
        "backfill-federal-reserve",
        "sync-federal-reserve",
        "clear-federal-reserve-cache",
        "validate-dol-mappings",
        "backfill-dol",
        "sync-dol",
        "clear-dol-cache",
        "audit-source-registry",
        "show-source-policy",
        "source-health",
        "validate-data-layer",
        "refresh-current",
    ]

    for command in commands:
        result = runner.invoke(cli.app, [command, "--help"])
        assert result.exit_code == 0, command
        assert "Usage:" in result.stdout


def test_census_backfill_month_parser_is_exact() -> None:
    assert cli._parse_month("2000-01", "--start") == date(2000, 1, 1)

    with pytest.raises(typer.BadParameter, match="YYYY-MM"):
        cli._parse_month("2000-01-15", "--start")


def test_show_source_policy_is_secret_free_json() -> None:
    result = runner.invoke(cli.app, ["show-source-policy", "US_REAL_GDP", "--json"])
    assert result.exit_code == 0
    assert '"primary_source": "BEA"' in result.stdout
    assert '"registry_hash"' in result.stdout
    assert "API_KEY" not in result.stdout


def test_show_source_policy_rejects_unknown_indicator() -> None:
    result = runner.invoke(cli.app, ["show-source-policy", "NOT_REAL"])
    assert result.exit_code == 1
    assert "Unknown source registry indicator" in result.stderr
