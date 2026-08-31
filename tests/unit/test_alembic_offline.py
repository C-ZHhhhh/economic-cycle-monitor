"""Offline Alembic migration rendering test."""

from io import StringIO

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def test_initial_migration_renders_postgresql_sql() -> None:
    output = StringIO()
    config = Config("alembic.ini", output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://macro:macro@localhost:5432/macro_test"
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE indicator_registry" in sql
    assert "CREATE TABLE agent_reports" in sql
    assert "CREATE TABLE indicator_source_mappings" in sql
    assert "CREATE TABLE source_comparison_results" in sql
    assert "ADD COLUMN source_parameters_json JSONB" in sql
    assert "JSONB" in sql


def test_all_revision_identifiers_fit_alembic_version_column() -> None:
    """Keep revision identifiers within Alembic's generated VARCHAR(32)."""
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert all(len(item.revision) <= 32 for item in scripts.walk_revisions())
