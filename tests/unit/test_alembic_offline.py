"""Offline Alembic migration rendering test."""

from io import StringIO

from alembic import command
from alembic.config import Config


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
    assert "JSONB" in sql
