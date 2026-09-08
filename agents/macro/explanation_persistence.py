"""Atomic persistence for validated explanation artifacts using existing tables."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from agents.macro.explanation_loader import (
    PERSISTED_CONTEXT_REPORT_TYPE,
    persisted_context_hash,
)
from agents.macro.explanation_schemas import ExplanationExecutionResult, PersistedExplanationContext
from app.database.models import AgentReport, AgentRun
from app.database.repositories.runs import add_run_with_report

SENSITIVE_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "credential", "password", "secret", "token"}
)


def _contains_sensitive_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in SENSITIVE_KEYS or _contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def persist_explanation_result(
    session: Session,
    result: ExplanationExecutionResult,
    *,
    git_commit: str | None = None,
    run_id: UUID | None = None,
) -> UUID:
    """Persist one snapshot and report atomically in the caller's transaction."""
    payload = result.model_dump(mode="json")
    if _contains_sensitive_key(payload):
        raise ValueError("SENSITIVE_EXPLANATION_PAYLOAD_REJECTED")
    now = datetime.now(UTC)
    resolved_run_id = run_id or uuid4()
    run = AgentRun(
        run_id=resolved_run_id,
        agent_name="us_macro_state_explanation_agent",
        agent_version=result.snapshot.snapshot_version,
        status="completed",
        started_at=now,
        finished_at=now,
        duration_seconds=Decimal(0),
        input_snapshot_hash=result.snapshot.snapshot_calculation_hash,
        data_quality_status=result.snapshot.regime_status.value.lower(),
        llm_mode=("mock" if result.provider == "mock" else "live" if result.provider else "none"),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost=Decimal(0),
        git_commit=git_commit,
    )
    report = AgentReport(
        run_id=resolved_run_id,
        report_type="macro_explanation_v1",
        result_json=payload,
        markdown_content=result.deterministic_report.markdown,
        llm_provider=result.provider,
        llm_model=result.model,
        prompt_version=result.prompt_version,
    )
    add_run_with_report(session, run, report)
    return resolved_run_id


def persist_explanation_context(
    session: Session,
    context: PersistedExplanationContext,
    *,
    git_commit: str | None = None,
    run_id: UUID | None = None,
) -> UUID:
    """Persist already-computed Stage 3 objects without invoking any Stage 3 service."""
    if context.persisted_context_hash != persisted_context_hash(context):
        raise ValueError("PERSISTED_EXPLANATION_CONTEXT_HASH_MISMATCH")
    payload = context.model_dump(mode="json")
    if _contains_sensitive_key(payload):
        raise ValueError("SENSITIVE_EXPLANATION_CONTEXT_REJECTED")
    resolved_run_id = run_id or uuid4()
    run = AgentRun(
        run_id=resolved_run_id,
        agent_name="stage3_persisted_explanation_context",
        agent_version=context.context_version,
        status="completed",
        started_at=context.materialized_at,
        finished_at=context.materialized_at,
        duration_seconds=Decimal(0),
        input_snapshot_hash=context.persisted_context_hash,
        data_quality_status=context.regime.regime_status.value.lower(),
        llm_mode="none",
        input_tokens=0,
        output_tokens=0,
        estimated_cost=Decimal(0),
        git_commit=git_commit,
    )
    report = AgentReport(
        run_id=resolved_run_id,
        report_type=PERSISTED_CONTEXT_REPORT_TYPE,
        result_json=payload,
        prompt_version=None,
    )
    add_run_with_report(session, run, report)
    return resolved_run_id
