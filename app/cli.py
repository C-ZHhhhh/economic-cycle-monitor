"""Command-line interface for Stage 1 and Stage 2 official-source operations."""

import asyncio
import json
import shutil
from collections.abc import Callable, Coroutine
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

import typer
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agents.macro.service import create_mock_run as create_mock_run_service
from app.data_pipeline.bls_service import BlsPipelineService
from app.data_pipeline.repository import finish_sync_run, start_sync_run
from app.data_pipeline.schemas import (
    BackfillOptions,
    BlsBackfillOptions,
    BlsSyncOptions,
    PipelineSelection,
    SourceComparisonOptions,
    SyncOptions,
    SyncSummary,
)
from app.data_pipeline.service import FredPipelineService
from app.data_pipeline.source_comparison import SourceComparisonService
from app.data_quality.service import run_database_quality_checks
from app.data_sources.bls.factory import create_bls_client
from app.data_sources.fred.factory import create_fred_client
from app.database.models import DataSourceRequest, IndicatorSourceMapping, SyncRun
from app.database.session import check_database, create_database_engine, db_session
from app.indicators.config import load_indicator_catalog
from app.indicators.repository import get_indicator
from app.indicators.repository import list_indicators as query_indicators
from app.indicators.service import seed_indicator_catalog
from app.settings import Settings
from app.source_mappings.config import load_source_mapping_catalog
from app.source_mappings.repository import seed_source_mappings

app = typer.Typer(
    name="economic-cycle-monitor",
    help="Operate the US Macro Cycle Agent data foundation.",
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
    """Validate Stage 2 configuration without opening network connections."""
    _load_settings()
    typer.echo("OK: configuration is valid for Stage 2")


def _require_bls_enabled(settings: Settings) -> None:
    if not settings.bls_enabled:
        _fail("BLS is disabled; set BLS_ENABLED=true before making BLS API requests")


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


def _render(value: object, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))
    else:
        typer.echo(value)


def _parse_date(value: str, option_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("must use YYYY-MM-DD", param_hint=option_name) from exc


@app.command("seed-indicators")
def seed_indicators(json_output: bool = typer.Option(False, "--json")) -> None:
    """Idempotently synchronize the versioned indicator catalog."""
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            catalog = load_indicator_catalog()
            run = start_sync_run(session, "seed_indicators", {}, len(catalog.indicators))
            result = seed_indicator_catalog(session, catalog)
            mapping_result = seed_source_mappings(session, load_source_mapping_catalog())
            summary = SyncSummary(
                run_id=run.run_id,
                operation=run.operation,
                status="succeeded",
                indicator_count=len(catalog.indicators),
                success_count=len(catalog.indicators),
                inserted_count=result.inserted,
                results=[result.model_dump(), {"bls_mappings": mapping_result.model_dump()}],
            )
            finish_sync_run(session, run, summary)
            payload = {
                "run_id": run.run_id,
                **result.model_dump(),
                "bls_mappings": mapping_result.model_dump(),
            }
    except (SQLAlchemyError, OSError, RuntimeError, ValueError):
        _fail("Indicator catalog could not be synchronized")
    finally:
        engine.dispose()
    _render(payload, json_output)


@app.command("list-indicators")
def list_indicator_command(
    module: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(False, "--enabled-only"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List registered indicators."""
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            rows = query_indicators(
                session, module=module, enabled=True if enabled_only else None, limit=100
            )
            payload = [
                {
                    "indicator_id": item.indicator_id,
                    "series_id": item.fred_series_id,
                    "module": item.module,
                    "enabled": item.enabled,
                    "metadata_status": item.metadata_status,
                }
                for item in rows
            ]
    except (SQLAlchemyError, OSError):
        _fail("Indicators could not be listed")
    finally:
        engine.dispose()
    _render(payload, json_output)


@app.command("show-indicator")
def show_indicator_command(
    indicator_id: str, json_output: bool = typer.Option(False, "--json")
) -> None:
    """Show one registered indicator and its metadata status."""
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            item = get_indicator(session, indicator_id)
            if item is None:
                _fail("Indicator was not found")
            payload = {
                "indicator_id": item.indicator_id,
                "name": item.name,
                "series_id": item.fred_series_id,
                "module": item.module,
                "enabled": item.enabled,
                "metadata_status": item.metadata_status,
                "metadata_warnings": item.metadata_warnings_json,
            }
    except (SQLAlchemyError, OSError):
        _fail("Indicator could not be loaded")
    finally:
        engine.dispose()
    _render(payload, json_output)


async def _validate_metadata(
    session: Session, settings: Settings, selection: PipelineSelection
) -> SyncSummary:
    async with create_fred_client(settings) as client:
        return await FredPipelineService(session, settings, client).validate_metadata(selection)


@app.command("validate-fred-metadata")
def validate_fred_metadata_command(
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    output: Path = typer.Option(Path("outputs/metadata_validation_report.json")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Fetch, persist, and validate registered FRED metadata."""
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            summary = asyncio.run(
                _validate_metadata(
                    session,
                    settings,
                    PipelineSelection(
                        indicator_id=indicator, module=module, enabled_only=enabled_only
                    ),
                )
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    except (SQLAlchemyError, OSError, RuntimeError, ValueError):
        _fail("FRED metadata validation failed")
    finally:
        engine.dispose()
    _render(summary.model_dump(mode="json"), json_output)
    if summary.status == "failed" or any(
        result.get("status") == "invalid" for result in summary.results
    ):
        raise typer.Exit(code=1)


async def _run_backfill(
    session: Session, settings: Settings, options: BackfillOptions
) -> SyncSummary:
    async with create_fred_client(settings) as client:
        return await FredPipelineService(session, settings, client).backfill(options)


@app.command("backfill-fred")
def backfill_fred_command(
    start: str = typer.Option("2000-01-01"),
    end: str = typer.Option(date.today().isoformat()),
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    force: bool = typer.Option(False),
    batch_size: int | None = typer.Option(None, min=100, max=5000),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Backfill validated FRED series over a bounded date range."""
    settings = _load_settings()
    if force:
        settings.fred_cache_enabled = False
    if batch_size is not None:
        settings.database_batch_size = batch_size
    options = BackfillOptions(
        indicator_id=indicator,
        module=module,
        enabled_only=enabled_only,
        start=_parse_date(start, "--start"),
        end=_parse_date(end, "--end"),
        force=force,
        dry_run=dry_run,
    )
    summary = _execute_async_pipeline(settings, _run_backfill, options, "FRED backfill failed")
    _render(summary.model_dump(mode="json"), json_output)
    if summary.status == "failed":
        raise typer.Exit(code=1)


async def _run_sync(session: Session, settings: Settings, options: SyncOptions) -> SyncSummary:
    async with create_fred_client(settings) as client:
        return await FredPipelineService(session, settings, client).sync(options)


@app.command("sync-fred")
def sync_fred_command(
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    since: str | None = typer.Option(None),
    lookback_days: int | None = typer.Option(None, min=1),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Incrementally fetch new and potentially revised observations."""
    settings = _load_settings()
    options = SyncOptions(
        indicator_id=indicator,
        module=module,
        since=_parse_date(since, "--since") if since else None,
        lookback_days=lookback_days,
        dry_run=dry_run,
    )
    summary = _execute_async_pipeline(settings, _run_sync, options, "FRED sync failed")
    _render(summary.model_dump(mode="json"), json_output)
    if summary.status == "failed":
        raise typer.Exit(code=1)


async def _run_vintage(
    session: Session, settings: Settings, options: tuple[str, date]
) -> SyncSummary:
    async with create_fred_client(settings) as client:
        return await FredPipelineService(session, settings, client).fetch_observations_as_known_on(
            *options
        )


async def _run_validate_bls(
    session: Session,
    settings: Settings,
    options: tuple[PipelineSelection, bool],
) -> SyncSummary:
    selection, dry_run = options
    async with create_bls_client(settings) as client:
        summary = await BlsPipelineService(session, settings, client).validate_mappings(selection)
    if dry_run:
        session.rollback()
    return summary


@app.command("validate-bls-mappings")
def validate_bls_mappings_command(
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate reviewed BLS mappings using one official batch request."""
    settings = _load_settings()
    _require_bls_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_validate_bls,
        (
            PipelineSelection(indicator_id=indicator, module=module, enabled_only=enabled_only),
            dry_run,
        ),
        "BLS mapping validation failed",
    )
    _render(summary.model_dump(mode="json"), json_output)
    if summary.status == "failed" or any(
        result.get("status") == "invalid" for result in summary.results
    ):
        raise typer.Exit(code=1)


async def _run_bls_backfill(
    session: Session, settings: Settings, options: BlsBackfillOptions
) -> SyncSummary:
    async with create_bls_client(settings) as client:
        return await BlsPipelineService(session, settings, client).backfill(options)


@app.command("backfill-bls")
def backfill_bls_command(
    start: int = typer.Option(2000, min=1913, max=2100),
    end: int = typer.Option(date.today().year, min=1913, max=2100),
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Backfill BLS history in configured inclusive year windows."""
    settings = _load_settings()
    _require_bls_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_bls_backfill,
        BlsBackfillOptions(
            indicator_id=indicator,
            module=module,
            enabled_only=enabled_only,
            start_year=start,
            end_year=end,
            dry_run=dry_run,
        ),
        "BLS backfill failed",
    )
    _render(summary.model_dump(mode="json"), json_output)
    if summary.status == "failed":
        raise typer.Exit(code=1)


async def _run_bls_sync(
    session: Session, settings: Settings, options: BlsSyncOptions
) -> SyncSummary:
    async with create_bls_client(settings) as client:
        return await BlsPipelineService(session, settings, client).sync(options)


@app.command("sync-bls")
def sync_bls_command(
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Incrementally synchronize BLS data with revision lookbacks."""
    settings = _load_settings()
    _require_bls_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_bls_sync,
        BlsSyncOptions(
            indicator_id=indicator,
            module=module,
            enabled_only=enabled_only,
            dry_run=dry_run,
        ),
        "BLS incremental synchronization failed",
    )
    _render(summary.model_dump(mode="json"), json_output)
    if summary.status == "failed":
        raise typer.Exit(code=1)


@app.command("compare-sources")
def compare_sources_command(
    primary: str = typer.Option("bls"),
    secondary: str = typer.Option("fred"),
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    start: str | None = typer.Option(None),
    end: str | None = typer.Option(None),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Compare independently stored BLS and FRED observations in pages."""
    primary_source = primary.lower()
    secondary_source = secondary.lower()
    if (primary_source, secondary_source) != ("bls", "fred"):
        _fail("Stage 2B-1 supports only --primary bls --secondary fred")
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            summary = SourceComparisonService(session).compare(
                SourceComparisonOptions(
                    primary_source=primary_source,
                    secondary_source=secondary_source,
                    indicator_id=indicator,
                    module=module,
                    start=_parse_date(start, "--start") if start else None,
                    end=_parse_date(end, "--end") if end else None,
                    dry_run=dry_run,
                )
            )
    except (SQLAlchemyError, OSError, RuntimeError, ValueError):
        _fail("Source comparison failed")
    finally:
        engine.dispose()
    _render(summary.model_dump(mode="json"), json_output)


PipelineOptions = (
    BackfillOptions
    | SyncOptions
    | BlsBackfillOptions
    | BlsSyncOptions
    | tuple[str, date]
    | tuple[PipelineSelection, bool]
)


def _execute_async_pipeline(
    settings: Settings,
    operation: Callable[..., Coroutine[Any, Any, SyncSummary]],
    options: PipelineOptions,
    error_message: str,
) -> SyncSummary:
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            return asyncio.run(operation(session, settings, options))
    except (SQLAlchemyError, OSError, RuntimeError, ValueError):
        _fail(error_message)
    finally:
        engine.dispose()


@app.command("fetch-fred-vintage")
def fetch_fred_vintage_command(
    indicator: str = typer.Option(...),
    vintage_date: str = typer.Option(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Fetch one indicator as it was known on a specific date."""
    settings = _load_settings()
    summary = _execute_async_pipeline(
        settings,
        _run_vintage,
        (indicator, _parse_date(vintage_date, "--vintage-date")),
        "FRED vintage fetch failed",
    )
    _render(summary.model_dump(mode="json"), json_output)
    if summary.status == "failed":
        raise typer.Exit(code=1)


@app.command("check-data-quality")
def check_data_quality_command(
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    since: str | None = typer.Option(None),
    output: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run Stage 2 data and metadata quality checks."""
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            summary = run_database_quality_checks(
                session,
                PipelineSelection(indicator_id=indicator, module=module),
                since=_parse_date(since, "--since") if since else None,
            )
            if output is not None:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    except (SQLAlchemyError, OSError, RuntimeError, ValueError):
        _fail("Data-quality checks failed")
    finally:
        engine.dispose()
    _render(summary.model_dump(mode="json"), json_output)


@app.command("show-sync-status")
def show_sync_status_command(
    limit: int = typer.Option(20, min=1, max=100),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show recent Stage 2 pipeline runs."""
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            rows = session.query(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit).all()
            payload = [
                {
                    "run_id": row.run_id,
                    "operation": row.operation,
                    "status": row.status,
                    "started_at": row.started_at,
                    "finished_at": row.finished_at,
                    "success_count": row.success_count,
                    "failure_count": row.failure_count,
                    "inserted_count": row.inserted_count,
                    "revised_count": row.revised_count,
                }
                for row in rows
            ]
    except (SQLAlchemyError, OSError):
        _fail("Sync status could not be loaded")
    finally:
        engine.dispose()
    _render(payload, json_output)


@app.command("show-source-status")
def show_source_status_command(
    source: str = typer.Argument("bls"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show secret-free BLS configuration, mapping, and request status."""
    normalized = source.lower()
    if normalized != "bls":
        _fail("Stage 2B-1 source status supports only BLS")
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            mappings = session.query(IndicatorSourceMapping).filter_by(source="bls").all()
            latest = (
                session.query(DataSourceRequest)
                .filter_by(source="bls")
                .order_by(DataSourceRequest.finished_at.desc())
                .first()
            )
            payload = {
                "source": "bls",
                "enabled": settings.bls_enabled,
                "api_key_configured": settings.bls_api_key is not None,
                "base_url": str(settings.bls_base_url),
                "mapping_count": len(mappings),
                "mapping_status_counts": {
                    status: sum(item.mapping_status == status for item in mappings)
                    for status in sorted({item.mapping_status for item in mappings})
                },
                "latest_request": (
                    {
                        "finished_at": latest.finished_at,
                        "success": latest.success,
                        "http_status": latest.http_status,
                        "from_cache": latest.from_cache,
                        "error_type": latest.error_type,
                    }
                    if latest is not None
                    else None
                ),
            }
    except (SQLAlchemyError, OSError):
        _fail("BLS source status could not be loaded")
    finally:
        engine.dispose()
    _render(payload, json_output)


@app.command("clear-fred-cache")
def clear_fred_cache_command() -> None:
    """Remove only the configured FRED cache directory."""
    settings = _load_settings()
    target = (settings.cache_dir / "fred").resolve()
    cache_root = settings.cache_dir.resolve()
    if target.parent != cache_root:
        _fail("Configured FRED cache path is unsafe")
    if target.exists():
        shutil.rmtree(target)
    typer.echo("OK: FRED cache is clear")


@app.command("clear-bls-cache")
def clear_bls_cache_command() -> None:
    """Remove only the configured BLS cache directory."""
    settings = _load_settings()
    target = (settings.cache_dir / "bls").resolve()
    cache_root = settings.cache_dir.resolve()
    if target.parent != cache_root:
        _fail("Configured BLS cache path is unsafe")
    if target.exists():
        shutil.rmtree(target)
    typer.echo("OK: BLS cache is clear")


if __name__ == "__main__":
    app()
