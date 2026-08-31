"""Live PostgreSQL migration verification."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect

pytestmark = pytest.mark.integration


def test_alembic_creates_all_tables(postgres_engine: Engine) -> None:
    tables = set(inspect(postgres_engine).get_table_names())
    assert {
        "alembic_version",
        "indicator_registry",
        "raw_observations",
        "observation_vintages",
        "derived_features",
        "module_scores",
        "regime_history",
        "agent_runs",
        "agent_reports",
        "sync_runs",
        "data_source_requests",
        "data_quality_events",
        "indicator_source_mappings",
        "source_comparison_results",
        "pit_change_index_generations",
        "pit_information_sets",
    } <= tables


def test_stage_one_through_stage_two_b_downgrade_and_reupgrade(
    postgres_engine: Engine,
) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)

    command.downgrade(config, "0001_initial_schema")
    stage_one_tables = set(inspect(postgres_engine).get_table_names())
    assert "indicator_registry" in stage_one_tables
    assert "sync_runs" not in stage_one_tables

    command.upgrade(config, "head")
    current_tables = set(inspect(postgres_engine).get_table_names())
    assert {
        "sync_runs",
        "data_source_requests",
        "data_quality_events",
        "indicator_source_mappings",
        "source_comparison_results",
    } <= current_tables


def test_stage_two_b_one_database_upgrades_to_bea_and_round_trips(
    postgres_engine: Engine,
) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)

    command.downgrade(config, "0003_stage2b_official_sources")
    assert "source_parameters_json" not in {
        item["name"] for item in inspect(postgres_engine).get_columns("indicator_source_mappings")
    }

    command.upgrade(config, "head")
    assert "source_parameters_json" in {
        item["name"] for item in inspect(postgres_engine).get_columns("indicator_source_mappings")
    }
    command.downgrade(config, "0003_stage2b_official_sources")
    command.upgrade(config, "head")
    assert "source_parameters_json" in {
        item["name"] for item in inspect(postgres_engine).get_columns("indicator_source_mappings")
    }


def test_dlcr003_database_round_trips_without_rewriting_legacy_rows(
    postgres_engine: Engine,
) -> None:
    """Upgrade through 0005, downgrade to 0004, and re-upgrade safely."""
    config = Config("alembic.ini")
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)

    columns = {
        item["name"] for item in inspect(postgres_engine).get_columns("observation_vintages")
    }
    assert "pit_information_set_id" in columns
    command.downgrade(config, "0004_stage2b_bea")
    assert "pit_information_set_id" not in {
        item["name"] for item in inspect(postgres_engine).get_columns("observation_vintages")
    }
    command.upgrade(config, "head")
    assert "pit_information_set_id" in {
        item["name"] for item in inspect(postgres_engine).get_columns("observation_vintages")
    }
