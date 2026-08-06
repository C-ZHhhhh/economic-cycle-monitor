"""Live PostgreSQL migration verification."""

import pytest
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
    } <= tables
