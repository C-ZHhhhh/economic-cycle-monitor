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
from app.data_pipeline.bea_service import BeaPipelineService
from app.data_pipeline.bls_service import BlsPipelineService
from app.data_pipeline.census_service import CensusPipelineService
from app.data_pipeline.dol_service import DolPipelineService
from app.data_pipeline.fed_service import FederalReservePipelineService
from app.data_pipeline.repository import finish_sync_run, start_sync_run
from app.data_pipeline.schemas import (
    BackfillOptions,
    BeaBackfillOptions,
    BeaSyncOptions,
    BlsBackfillOptions,
    BlsSyncOptions,
    CensusBackfillOptions,
    CensusSelection,
    CensusSyncOptions,
    PipelineSelection,
    SourceComparisonOptions,
    SyncOptions,
    SyncSummary,
)
from app.data_pipeline.service import FredPipelineService
from app.data_pipeline.source_comparison import SourceComparisonService
from app.data_quality.service import run_database_quality_checks
from app.data_sources.bea.factory import create_bea_client
from app.data_sources.bls.factory import create_bls_client
from app.data_sources.census.factory import create_census_client
from app.data_sources.dol.factory import create_dol_client
from app.data_sources.federal_reserve.factory import create_federal_reserve_client
from app.data_sources.fred.factory import create_fred_client
from app.database.models import DataSourceRequest, IndicatorSourceMapping, SyncRun
from app.database.session import check_database, create_database_engine, db_session
from app.indicators.config import load_indicator_catalog
from app.indicators.repository import get_indicator
from app.indicators.repository import list_indicators as query_indicators
from app.indicators.service import seed_indicator_catalog
from app.settings import Settings
from app.source_mappings.config import (
    load_source_mapping_catalog,
    load_source_mapping_catalog_for,
)
from app.source_mappings.repository import seed_source_mappings
from app.source_registry.audit import audit_materialized_policy, audit_source_registry
from app.source_registry.config import load_source_registry
from app.source_registry.health import indicator_source_health, provider_health
from app.source_registry.readiness import validate_data_layer as validate_data_layer_service
from app.validation.config import load_validation_catalog
from app.validation.service import HistoricalValidationService

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


def _require_bea_enabled(settings: Settings) -> None:
    if not settings.bea_enabled:
        _fail("BEA is disabled; set BEA_ENABLED=true before making BEA API requests")
    if settings.bea_api_key is None:
        _fail("BEA API key is not configured")


def _require_census_enabled(settings: Settings) -> None:
    if not settings.census_enabled:
        _fail("Census is disabled; set CENSUS_ENABLED=true before making Census API requests")
    if settings.census_api_key is None:
        _fail("Census API key is not configured")


def _require_federal_reserve_enabled(settings: Settings) -> None:
    if not settings.federal_reserve_enabled:
        _fail("Federal Reserve is disabled; set FEDERAL_RESERVE_ENABLED=true")


def _require_dol_enabled(settings: Settings) -> None:
    if not settings.dol_enabled:
        _fail("DOL is disabled; set DOL_ENABLED=true")


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


def _parse_month(value: str, option_name: str) -> date:
    """Parse an inclusive Census month without accepting day-level ambiguity."""
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise typer.BadParameter("must use YYYY-MM", param_hint=option_name) from exc
    if parsed.year < 1959 or parsed.year > 2100:
        raise typer.BadParameter("year must be between 1959 and 2100", param_hint=option_name)
    return parsed


@app.command("seed-indicators")
def seed_indicators(json_output: bool = typer.Option(False, "--json")) -> None:
    """Idempotently synchronize the versioned indicator catalog."""
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            catalog = load_indicator_catalog()
            registry = load_source_registry()
            run = start_sync_run(
                session,
                "seed_indicators",
                {
                    "registry_policy_version": registry.registry_policy_version,
                    "registry_hash": registry.registry_hash,
                },
                len(catalog.indicators),
            )
            result = seed_indicator_catalog(session, catalog, registry)
            bls_mapping_result = seed_source_mappings(
                session, load_source_mapping_catalog(), registry
            )
            bea_mapping_result = seed_source_mappings(
                session, load_source_mapping_catalog_for("bea"), registry
            )
            census_mapping_result = seed_source_mappings(
                session, load_source_mapping_catalog_for("census"), registry
            )
            fed_mapping_result = seed_source_mappings(
                session, load_source_mapping_catalog_for("federal_reserve"), registry
            )
            dol_mapping_result = seed_source_mappings(
                session, load_source_mapping_catalog_for("dol"), registry
            )
            summary = SyncSummary(
                run_id=run.run_id,
                operation=run.operation,
                status="succeeded",
                indicator_count=len(catalog.indicators),
                success_count=len(catalog.indicators),
                inserted_count=result.inserted,
                results=[
                    result.model_dump(),
                    {"bls_mappings": bls_mapping_result.model_dump()},
                    {"bea_mappings": bea_mapping_result.model_dump()},
                    {"census_mappings": census_mapping_result.model_dump()},
                    {"federal_reserve_mappings": fed_mapping_result.model_dump()},
                    {"dol_mappings": dol_mapping_result.model_dump()},
                ],
            )
            finish_sync_run(session, run, summary)
            payload = {
                "run_id": run.run_id,
                "registry_policy_version": registry.registry_policy_version,
                "registry_hash": registry.registry_hash,
                **result.model_dump(),
                "bls_mappings": bls_mapping_result.model_dump(),
                "bea_mappings": bea_mapping_result.model_dump(),
                "census_mappings": census_mapping_result.model_dump(),
                "federal_reserve_mappings": fed_mapping_result.model_dump(),
                "dol_mappings": dol_mapping_result.model_dump(),
            }
    except (SQLAlchemyError, OSError, RuntimeError, ValueError):
        _fail("Indicator catalog could not be synchronized")
    finally:
        engine.dispose()
    _render(payload, json_output)


@app.command("audit-source-registry")
def audit_source_registry_command(
    strict: bool = typer.Option(False, "--strict"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate registry completeness and database materialization drift."""
    registry = load_source_registry()
    result = audit_source_registry(
        registry,
        load_indicator_catalog(),
        [
            load_source_mapping_catalog_for(source)
            for source in ("bls", "bea", "census", "federal_reserve", "dol")
        ],
    )
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            materialized = audit_materialized_policy(
                registry, list(query_indicators(session, limit=100))
            )
    except (SQLAlchemyError, OSError, RuntimeError):
        _fail("Source registry database audit failed")
    finally:
        engine.dispose()
    payload = {
        **result.model_dump(mode="json"),
        "passed": result.passed and not materialized,
        "materialized_issues": [item.model_dump(mode="json") for item in materialized],
    }
    _render(payload, json_output)
    if strict and not payload["passed"]:
        raise typer.Exit(code=1)


@app.command("show-source-policy")
def show_source_policy_command(
    indicator: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show one versioned source-resolution policy without runtime secrets."""
    registry = load_source_registry()
    policy = registry.by_indicator().get(indicator)
    if policy is None:
        _fail("Unknown source registry indicator")
    _render(
        {
            "registry_hash": registry.registry_hash,
            **policy.model_dump(mode="json"),
        },
        json_output,
    )


@app.command("source-health")
def source_health_command(
    indicator: str | None = typer.Option(None, "--indicator"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Compute provider and indicator/source health from runtime evidence."""
    settings = _load_settings()
    registry = load_source_registry()
    policies = registry.by_indicator()
    if indicator is not None and indicator not in policies:
        _fail("Unknown source registry indicator")
    selected = [policies[indicator]] if indicator is not None else registry.policies
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            provider_sources = sorted({policy.primary_source.value for policy in selected})
            payload = {
                "providers": [
                    provider_health(
                        session,
                        {
                            "FRED_CURRENT": "fred",
                            "BLS": "bls",
                            "BEA": "bea",
                            "CENSUS": "census",
                            "FEDERAL_RESERVE": "federal_reserve",
                            "DOL": "dol",
                        }[source],
                    ).model_dump(mode="json")
                    for source in provider_sources
                ],
                "indicators": [
                    indicator_source_health(session, policy).model_dump(mode="json")
                    for policy in selected
                ],
            }
    except (SQLAlchemyError, OSError, RuntimeError, ValueError):
        _fail("Source health could not be computed")
    finally:
        engine.dispose()
    _render(payload, json_output)


@app.command("validate-data-layer")
def validate_data_layer_command(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Evaluate CURRENT_DATA_READY separately from PIT_BACKTEST_READY."""
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            result = validate_data_layer_service(session)
    except (SQLAlchemyError, OSError, RuntimeError, ValueError):
        _fail("Data-layer readiness validation failed")
    finally:
        engine.dispose()
    _render(result.model_dump(mode="json"), json_output)
    if not result.current_data_ready:
        raise typer.Exit(code=1)


@app.command("validate-stage3f-config")
def validate_stage3f_config_command(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate the Stage 3F authority without database or network access."""
    try:
        catalog = load_validation_catalog()
    except (OSError, RuntimeError, ValueError, ValidationError):
        _fail("Stage 3F validation authority is invalid")
    _render(
        {
            "validation_spec_version": catalog.validation_spec_version,
            "validation_spec_hash": catalog.specification_hash,
            "validation_phase": catalog.validation_phase.value,
            "full_historical_run_approved": (catalog.budgets.provisional_full_run.approved),
        },
        json_output,
    )


@app.command("run-stage3f-pilot")
def run_stage3f_pilot_command(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run the approved bounded date-EOD Stage 3F pilot only."""
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            summary = asyncio.run(HistoricalValidationService(session, settings).run_pilot())
    except (SQLAlchemyError, OSError, RuntimeError, ValueError):
        _fail("Stage 3F bounded pilot stopped safely; inspect the local checkpoint")
    finally:
        engine.dispose()
    _render(summary.model_dump(mode="json"), json_output)


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


async def _run_validate_bea(
    session: Session,
    settings: Settings,
    options: tuple[PipelineSelection, bool],
) -> SyncSummary:
    selection, dry_run = options
    async with create_bea_client(settings) as client:
        summary = await BeaPipelineService(session, settings, client).validate_mappings(selection)
    if dry_run:
        session.rollback()
    return summary


@app.command("validate-bea-mappings")
def validate_bea_mappings_command(
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate exact BEA mappings without auto-promoting pending candidates."""
    settings = _load_settings()
    _require_bea_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_validate_bea,
        (
            PipelineSelection(indicator_id=indicator, module=module, enabled_only=enabled_only),
            dry_run,
        ),
        "BEA mapping validation failed",
    )
    _render(summary.model_dump(mode="json"), json_output)
    if summary.failure_count:
        raise typer.Exit(code=1)


async def _run_bea_backfill(
    session: Session, settings: Settings, options: BeaBackfillOptions
) -> SyncSummary:
    async with create_bea_client(settings) as client:
        return await BeaPipelineService(session, settings, client).backfill(options)


@app.command("backfill-bea")
def backfill_bea_command(
    start_year: int = typer.Option(2000, min=1929, max=2100),
    end_year: int = typer.Option(date.today().year, min=1929, max=2100),
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Backfill BEA NIPA history using small explicit year batches."""
    settings = _load_settings()
    _require_bea_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_bea_backfill,
        BeaBackfillOptions(
            indicator_id=indicator,
            module=module,
            enabled_only=enabled_only,
            start_year=start_year,
            end_year=end_year,
            dry_run=dry_run,
        ),
        "BEA backfill failed",
    )
    _render(summary.model_dump(mode="json"), json_output)
    if summary.status == "failed":
        raise typer.Exit(code=1)


async def _run_bea_sync(
    session: Session, settings: Settings, options: BeaSyncOptions
) -> SyncSummary:
    async with create_bea_client(settings) as client:
        return await BeaPipelineService(session, settings, client).sync(options)


@app.command("sync-bea")
def sync_bea_command(
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Incrementally synchronize BEA data with configured revision lookbacks."""
    settings = _load_settings()
    _require_bea_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_bea_sync,
        BeaSyncOptions(
            indicator_id=indicator,
            module=module,
            enabled_only=enabled_only,
            dry_run=dry_run,
        ),
        "BEA incremental synchronization failed",
    )
    _render(summary.model_dump(mode="json"), json_output)
    if summary.status == "failed":
        raise typer.Exit(code=1)


async def _run_validate_census(
    session: Session,
    settings: Settings,
    options: tuple[CensusSelection, bool],
) -> SyncSummary:
    selection, dry_run = options
    async with create_census_client(settings) as client:
        summary = await CensusPipelineService(session, settings, client).validate_mappings(
            selection
        )
    if dry_run:
        session.rollback()
    return summary


@app.command("validate-census-mappings")
def validate_census_mappings_command(
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    dataset: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate Census candidate dimensions without promoting pending mappings."""
    settings = _load_settings()
    _require_census_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_validate_census,
        (
            CensusSelection(
                indicator_id=indicator,
                module=module,
                enabled_only=enabled_only,
                dataset=dataset,
            ),
            dry_run,
        ),
        "Census mapping validation failed",
    )
    _render(summary.model_dump(mode="json"), json_output)
    if summary.failure_count:
        raise typer.Exit(code=1)


async def _run_census_backfill(
    session: Session, settings: Settings, options: CensusBackfillOptions
) -> SyncSummary:
    async with create_census_client(settings) as client:
        return await CensusPipelineService(session, settings, client).backfill(options)


@app.command("backfill-census")
def backfill_census_command(
    start: str = typer.Option("2000-01"),
    end: str = typer.Option(date.today().strftime("%Y-%m")),
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    dataset: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Backfill only live-reviewed DIRECT Census mappings."""
    settings = _load_settings()
    _require_census_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_census_backfill,
        CensusBackfillOptions(
            indicator_id=indicator,
            module=module,
            enabled_only=enabled_only,
            dataset=dataset,
            start=_parse_month(start, "--start"),
            end=_parse_month(end, "--end"),
            dry_run=dry_run,
        ),
        "Census backfill failed",
    )
    _render(summary.model_dump(mode="json"), json_output)
    if summary.status == "failed":
        raise typer.Exit(code=1)


async def _run_census_sync(
    session: Session, settings: Settings, options: CensusSyncOptions
) -> SyncSummary:
    async with create_census_client(settings) as client:
        return await CensusPipelineService(session, settings, client).sync(options)


@app.command("sync-census")
def sync_census_command(
    indicator: str | None = typer.Option(None),
    module: str | None = typer.Option(None),
    dataset: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Incrementally synchronize reviewed DIRECT Census mappings."""
    settings = _load_settings()
    _require_census_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_census_sync,
        CensusSyncOptions(
            indicator_id=indicator,
            module=module,
            enabled_only=enabled_only,
            dataset=dataset,
            dry_run=dry_run,
        ),
        "Census incremental synchronization failed",
    )
    _render(summary.model_dump(mode="json"), json_output)
    if summary.status == "failed":
        raise typer.Exit(code=1)


async def _run_validate_federal_reserve(
    session: Session, settings: Settings, options: tuple[PipelineSelection, bool]
) -> SyncSummary:
    selection, dry_run = options
    async with create_federal_reserve_client(settings) as client:
        summary = await FederalReservePipelineService(session, settings, client).validate_mappings(
            selection
        )
    if dry_run:
        session.rollback()
    return summary


@app.command("validate-federal-reserve-mappings")
def validate_federal_reserve_mappings_command(
    indicator: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate Board release identities without promoting pending candidates."""
    settings = _load_settings()
    _require_federal_reserve_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_validate_federal_reserve,
        (PipelineSelection(indicator_id=indicator, enabled_only=enabled_only), dry_run),
        "Federal Reserve mapping validation failed",
    )
    _render(summary.model_dump(mode="json"), json_output)
    if summary.failure_count:
        raise typer.Exit(code=1)


async def _run_federal_reserve_backfill(
    session: Session, settings: Settings, options: BackfillOptions
) -> SyncSummary:
    async with create_federal_reserve_client(settings) as client:
        return await FederalReservePipelineService(session, settings, client).backfill(options)


@app.command("backfill-federal-reserve")
def backfill_federal_reserve_command(
    start: str = typer.Option("2000-01-01"),
    end: str = typer.Option(date.today().isoformat()),
    indicator: str | None = typer.Option(None),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reconcile reviewed Board release histories without DDP packages."""
    settings = _load_settings()
    _require_federal_reserve_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_federal_reserve_backfill,
        BackfillOptions(
            indicator_id=indicator,
            start=_parse_date(start, "--start"),
            end=_parse_date(end, "--end"),
            dry_run=dry_run,
        ),
        "Federal Reserve backfill failed",
    )
    _render(summary.model_dump(mode="json"), json_output)


async def _run_federal_reserve_sync(
    session: Session, settings: Settings, options: tuple[PipelineSelection, bool]
) -> SyncSummary:
    selection, dry_run = options
    async with create_federal_reserve_client(settings) as client:
        return await FederalReservePipelineService(session, settings, client).sync(
            selection, dry_run=dry_run
        )


@app.command("sync-federal-reserve")
def sync_federal_reserve_command(
    indicator: str | None = typer.Option(None),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Synchronize reviewed Board G.17/G.19 mappings."""
    settings = _load_settings()
    _require_federal_reserve_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_federal_reserve_sync,
        (PipelineSelection(indicator_id=indicator), dry_run),
        "Federal Reserve sync failed",
    )
    _render(summary.model_dump(mode="json"), json_output)


async def _run_validate_dol(
    session: Session, settings: Settings, options: tuple[PipelineSelection, bool]
) -> SyncSummary:
    selection, dry_run = options
    async with create_dol_client(settings) as client:
        summary = await DolPipelineService(session, settings, client).validate_mappings(selection)
    if dry_run:
        session.rollback()
    return summary


@app.command("validate-dol-mappings")
def validate_dol_mappings_command(
    indicator: str | None = typer.Option(None),
    enabled_only: bool = typer.Option(True, "--enabled-only/--all"),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Collect DOL schema evidence while strict XML paths remain pending."""
    settings = _load_settings()
    _require_dol_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_validate_dol,
        (PipelineSelection(indicator_id=indicator, enabled_only=enabled_only), dry_run),
        "DOL mapping validation failed",
    )
    _render(summary.model_dump(mode="json"), json_output)


async def _run_dol_backfill(
    session: Session, settings: Settings, options: BackfillOptions
) -> SyncSummary:
    async with create_dol_client(settings) as client:
        return await DolPipelineService(session, settings, client).backfill(options)


@app.command("backfill-dol")
def backfill_dol_command(
    start: str = typer.Option("2000-01-01"),
    end: str = typer.Option(date.today().isoformat()),
    indicator: str | None = typer.Option(None),
    dry_run: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Backfill only Live-reviewed DOL Weekly Claims mappings."""
    settings = _load_settings()
    _require_dol_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_dol_backfill,
        BackfillOptions(
            indicator_id=indicator,
            start=_parse_date(start, "--start"),
            end=_parse_date(end, "--end"),
            dry_run=dry_run,
        ),
        "DOL backfill failed",
    )
    _render(summary.model_dump(mode="json"), json_output)


async def _run_dol_sync(
    session: Session,
    settings: Settings,
    options: tuple[PipelineSelection, bool, bool],
) -> SyncSummary:
    selection, dry_run, annual_reconciliation = options
    async with create_dol_client(settings) as client:
        return await DolPipelineService(session, settings, client).sync(
            selection,
            dry_run=dry_run,
            annual_reconciliation=annual_reconciliation,
        )


@app.command("sync-dol")
def sync_dol_command(
    indicator: str | None = typer.Option(None),
    dry_run: bool = typer.Option(False),
    annual_reconciliation: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Synchronize the configured DOL revision/reconciliation window."""
    settings = _load_settings()
    _require_dol_enabled(settings)
    summary = _execute_async_pipeline(
        settings,
        _run_dol_sync,
        (PipelineSelection(indicator_id=indicator), dry_run, annual_reconciliation),
        "DOL sync failed",
    )
    _render(summary.model_dump(mode="json"), json_output)


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
    """Compare independently stored official and FRED observations in pages."""
    primary_source = primary.lower()
    secondary_source = secondary.lower()
    if (
        primary_source
        not in {
            "bls",
            "bea",
            "census",
            "federal_reserve",
            "dol",
        }
        or secondary_source != "fred"
    ):
        _fail(
            "Supported comparisons use --primary "
            "bls|bea|census|federal_reserve|dol --secondary fred"
        )
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
    | BeaBackfillOptions
    | BeaSyncOptions
    | CensusBackfillOptions
    | CensusSyncOptions
    | tuple[str, date]
    | tuple[PipelineSelection, bool]
    | tuple[PipelineSelection, bool, bool]
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
    """Show secret-free official-source configuration, mapping, and request status."""
    normalized = source.lower()
    if normalized not in {"bls", "bea", "census", "federal_reserve", "dol"}:
        _fail("Unsupported official source")
    settings = _load_settings()
    engine = create_database_engine(settings.database_url)
    try:
        with db_session(engine) as session:
            mappings = session.query(IndicatorSourceMapping).filter_by(source=normalized).all()
            latest = (
                session.query(DataSourceRequest)
                .filter_by(source=normalized)
                .order_by(DataSourceRequest.finished_at.desc())
                .first()
            )
            payload = {
                "source": normalized,
                "enabled": getattr(settings, f"{normalized}_enabled"),
                "credentials_required": normalized not in {"federal_reserve", "dol"},
                "api_key_configured": (
                    getattr(settings, f"{normalized}_api_key") is not None
                    if normalized not in {"federal_reserve", "dol"}
                    else False
                ),
                "base_url": str(getattr(settings, f"{normalized}_base_url")),
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
        _fail("Official-source status could not be loaded")
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


@app.command("clear-bea-cache")
def clear_bea_cache_command() -> None:
    """Remove only the configured BEA cache directory."""
    settings = _load_settings()
    target = (settings.cache_dir / "bea").resolve()
    cache_root = settings.cache_dir.resolve()
    if target.parent != cache_root:
        _fail("Configured BEA cache path is unsafe")
    if target.exists():
        shutil.rmtree(target)
    typer.echo("OK: BEA cache is clear")


@app.command("clear-census-cache")
def clear_census_cache_command() -> None:
    """Remove only the configured Census cache directory."""
    settings = _load_settings()
    target = (settings.cache_dir / "census").resolve()
    cache_root = settings.cache_dir.resolve()
    if target.parent != cache_root:
        _fail("Configured Census cache path is unsafe")
    if target.exists():
        shutil.rmtree(target)
    typer.echo("OK: Census cache is clear")


def _clear_public_cache(source: str) -> None:
    settings = _load_settings()
    target = (settings.cache_dir / source).resolve()
    cache_root = settings.cache_dir.resolve()
    if target.parent != cache_root:
        _fail("Configured cache path is unsafe")
    if target.exists():
        shutil.rmtree(target)
    typer.echo(f"OK: {source} cache is clear")


@app.command("clear-federal-reserve-cache")
def clear_federal_reserve_cache_command() -> None:
    """Remove only the configured Board release cache directory."""
    _clear_public_cache("federal_reserve")


@app.command("clear-dol-cache")
def clear_dol_cache_command() -> None:
    """Remove only the configured DOL cache directory."""
    _clear_public_cache("dol")


if __name__ == "__main__":
    app()
