"""Database metadata and PostgreSQL DDL tests."""

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, dialect
from sqlalchemy.schema import CreateTable

from app.database.base import Base
from app.database.models import AgentReport, RawObservation


def test_metadata_contains_stage_one_and_stage_two_tables() -> None:
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
    }


def test_json_results_use_postgresql_jsonb() -> None:
    assert isinstance(AgentReport.__table__.c.result_json.type, JSONB)


def test_raw_observation_unique_constraint_and_postgres_ddl() -> None:
    names = {
        constraint.name
        for constraint in RawObservation.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_raw_observation_key" in names
    ddl = str(CreateTable(RawObservation.__table__).compile(dialect=dialect()))
    assert "UNIQUE (indicator_id, observation_date, source)" in ddl
