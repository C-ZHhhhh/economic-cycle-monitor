"""PostgreSQL persistence and read-only API tests for explanation v1."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from agents.macro.explanation_config import load_explanation_authority
from agents.macro.explanation_llm import MockStructuredExplanationClient
from agents.macro.explanation_loader import PersistedExplanationContextLoader
from agents.macro.explanation_persistence import (
    persist_explanation_context,
    persist_explanation_result,
)
from agents.macro.explanation_schemas import (
    DependencyAvailability,
    PersistedCadence,
    PersistedExplanationContext,
)
from agents.macro.explanation_service import MacroExplanationService
from app.composites.schemas import EvaluationMode
from app.database.models import AgentRun, DataSourceRequest, PitInformationSet
from app.main import create_app
from app.settings import Settings
from tests.unit.test_macro_explanation import StaticLoader, _context, _loaded

pytestmark = pytest.mark.integration


def _persist_context(session: Session, context: PersistedExplanationContext) -> None:
    persist_explanation_context(session, context)


def test_loader_selects_strict_previous_canonical_rows(postgres_session: Session) -> None:
    release = asyncio.run(_context(None, cadence=PersistedCadence.RELEASE_EVENT_EOD))
    month = release.model_copy(
        update={
            "cadence": PersistedCadence.MONTH_END_EOD,
            "persisted_context_hash": "pending",
        }
    )
    from agents.macro.explanation_loader import persisted_context_hash

    month = month.model_copy(update={"persisted_context_hash": persisted_context_hash(month)})
    _persist_context(postgres_session, release)
    _persist_context(postgres_session, month)
    loaded = PersistedExplanationContextLoader(postgres_session).load(
        evaluation_mode=EvaluationMode.CURRENT
    )
    assert loaded.status is DependencyAvailability.AVAILABLE
    assert loaded.current is not None
    assert loaded.previous_release_eod is None
    assert loaded.previous_month_end is None


def test_persist_result_and_read_only_http(postgres_engine: Engine) -> None:
    context = asyncio.run(_context())
    authority = load_explanation_authority()
    service = MacroExplanationService(Session(), authority, loader=StaticLoader(_loaded(context)))
    snapshot = service.build_snapshot(evaluation_mode=EvaluationMode.CURRENT)
    result = asyncio.run(service.explain(snapshot, client=MockStructuredExplanationClient()))
    with Session(postgres_engine) as session:
        run_id = persist_explanation_result(session, result, git_commit="f" * 40)
        session.commit()

    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=postgres_engine.url.render_as_string(hide_password=False),
        llm_mode="mock",
    )
    with TestClient(create_app(settings)) as client:
        latest = client.get("/api/v1/macro/explanations/latest")
        detail = client.get(f"/api/v1/macro/explanations/{run_id}")
        legacy = client.get("/api/v1/macro/schema")
        schema = client.get("/api/v1/macro/explanations/schema")
    assert latest.status_code == 200
    assert detail.status_code == 200
    assert detail.json()["run_id"] == str(run_id)
    assert detail.json()["result_json"]["snapshot"]["snapshot_version"] == ("agent-explanation-v1")
    assert "regime_probabilities" in legacy.json()["properties"]
    assert "snapshot_calculation_hash" in schema.json()["properties"]


def test_sensitive_payload_rejected_before_database_write(postgres_session: Session) -> None:
    context = asyncio.run(_context())
    authority = load_explanation_authority()
    service = MacroExplanationService(Session(), authority, loader=StaticLoader(_loaded(context)))
    snapshot = service.build_snapshot(evaluation_mode=EvaluationMode.CURRENT)
    result = asyncio.run(service.explain(snapshot)).model_copy(
        update={"generation_settings": {"api_key": "must-not-persist"}}
    )
    before = postgres_session.scalar(select(func.count()).select_from(AgentRun))
    with pytest.raises(ValueError, match="SENSITIVE"):
        persist_explanation_result(postgres_session, result)
    after = postgres_session.scalar(select(func.count()).select_from(AgentRun))
    assert before == after


def test_snapshot_build_has_zero_request_and_materialization_delta(
    postgres_session: Session,
) -> None:
    context = asyncio.run(_context())
    request_before = postgres_session.scalar(select(func.count()).select_from(DataSourceRequest))
    pit_before = postgres_session.scalar(select(func.count()).select_from(PitInformationSet))
    snapshot = MacroExplanationService(
        postgres_session,
        load_explanation_authority(),
        loader=StaticLoader(_loaded(context)),
    ).build_snapshot(evaluation_mode=EvaluationMode.CURRENT)
    request_after = postgres_session.scalar(select(func.count()).select_from(DataSourceRequest))
    pit_after = postgres_session.scalar(select(func.count()).select_from(PitInformationSet))
    assert snapshot.snapshot_calculation_hash
    assert request_after == request_before
    assert pit_after == pit_before
