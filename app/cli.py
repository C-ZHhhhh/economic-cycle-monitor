"""Command-line interface for Stage 1 operations."""

import asyncio
import json
from typing import NoReturn

import typer
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from agents.macro.service import create_mock_run as create_mock_run_service
from app.database.session import check_database, create_database_engine, db_session
from app.settings import Settings

app = typer.Typer(
    name="economic-cycle-monitor",
    help="Operate the Stage 1 US Macro Cycle Agent foundation.",
    no_args_is_help=True,
)


def _fail(message: str) -> NoReturn:
    typer.echo(f"ERROR: {message}", err=True)
    raise typer.Exit(code=1)


def _load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        _fail(f"Configuration is invalid ({exc.error_count()} validation error(s))")


@app.command()
def health() -> None:
    """Check validated configuration and PostgreSQL connectivity."""
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        check_database(engine)
    except (SQLAlchemyError, OSError):
        _fail("Database is unavailable")
    finally:
        engine.dispose()
    typer.echo("OK: application configuration and database are healthy")


@app.command("init-db")
def init_db() -> None:
    """Apply all Alembic migrations to the configured database."""
    settings = _load_settings()
    config = Config("alembic.ini")
    config.attributes["database_url"] = settings.database_url
    try:
        command.upgrade(config, "head")
    except (CommandError, SQLAlchemyError, OSError, RuntimeError):
        _fail("Database migration failed")
    typer.echo("OK: database is at the latest migration")


@app.command("show-config")
def show_config() -> None:
    """Display only allow-listed and redacted configuration values."""
    settings = _load_settings()
    typer.echo(json.dumps(settings.safe_summary(), indent=2, sort_keys=True, default=str))


@app.command("validate-config")
def validate_config() -> None:
    """Validate Stage 1 configuration without opening network connections."""
    _load_settings()
    typer.echo("OK: configuration is valid for Stage 1")


@app.command("create-mock-run")
def create_mock_run() -> None:
    """Create one validated mock run and report in PostgreSQL."""
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            result = asyncio.run(create_mock_run_service(session, settings))
    except (SQLAlchemyError, OSError, RuntimeError, ValueError):
        _fail("Mock run could not be created")
    finally:
        engine.dispose()
    typer.echo(f"OK: created mock run {result.run_id}")


if __name__ == "__main__":
    app()
