"""Read-only API for persisted agent runs."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.dependencies import get_request_session
from app.api.errors import APIError
from app.database.repositories.runs import get_reports_for_run, get_run, list_runs

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_request_session)]


class AgentRunResponse(BaseModel):
    """Public representation of one agent execution."""

    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    agent_name: str
    agent_version: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: Decimal | None
    data_quality_status: str | None
    llm_mode: str
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: Decimal | None
    git_commit: str | None
    created_at: datetime


class AgentReportResponse(BaseModel):
    """Public representation of one persisted report."""

    model_config = ConfigDict(from_attributes=True)

    report_type: str
    result_json: dict[str, Any]
    markdown_content: str | None
    html_content: str | None
    llm_provider: str | None
    llm_model: str | None
    prompt_version: str | None
    created_at: datetime


class RunDetailResponse(BaseModel):
    """Run metadata and all associated reports."""

    run: AgentRunResponse
    reports: list[AgentReportResponse]


@router.get("", response_model=list[AgentRunResponse])
def runs_index(
    session: SessionDependency,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[AgentRunResponse]:
    """List recent agent executions."""
    return [
        AgentRunResponse.model_validate(run)
        for run in list_runs(session, offset=offset, limit=limit)
    ]


@router.get("/{run_id}", response_model=RunDetailResponse)
def run_detail(
    run_id: UUID,
    session: SessionDependency,
) -> RunDetailResponse:
    """Return one run and its structured reports."""
    run = get_run(session, run_id)
    if run is None:
        raise APIError(status_code=404, code="run_not_found", message="Agent run was not found")
    reports = get_reports_for_run(session, run_id)
    return RunDetailResponse(
        run=AgentRunResponse.model_validate(run),
        reports=[AgentReportResponse.model_validate(report) for report in reports],
    )
