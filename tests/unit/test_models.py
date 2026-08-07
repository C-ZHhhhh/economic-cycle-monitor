"""Database metadata and PostgreSQL DDL tests."""

from typing import cast

from sqlalchemy import Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, dialect
from sqlalchemy.schema import CreateTable

from app.database.base import Base
from app.database.models import AgentReport, RawObservation


def test_metadata_contains_stage_one_through_stage_two_b_tables() -> None:
    assert set(Base.metadata.tables) == {
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
    }


def test_json_results_use_postgresql_jsonb() -> None:
    assert isinstance(AgentReport.__table__.c.result_json.type, JSONB)


def test_raw_observation_unique_constraint_and_postgres_ddl() -> None:
    table = cast(Table, RawObservation.__table__)
    names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_raw_observation_key" in names
    ddl = str(CreateTable(table).compile(dialect=dialect()))  # type: ignore[no-untyped-call]
    assert "UNIQUE (indicator_id, observation_date, source)" in ddl
