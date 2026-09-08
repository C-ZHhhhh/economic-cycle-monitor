"""US macro-agent contract and persisted explanation routes."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from agents.macro.explanation_schemas import MacroExplanationSnapshot
from agents.macro.schemas import MacroCycleResult
from app.api.dependencies import get_request_session
from app.api.errors import APIError
from app.database.repositories.runs import (
    get_latest_report_by_type,
    get_report_for_run_type,
)

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_request_session)]
EXPLANATION_REPORT_TYPE = "macro_explanation_v1"


class PersistedExplanationResponse(BaseModel):
    """Read-only public representation of one validated explanation artifact."""

    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    result_json: dict[str, Any]
    markdown_content: str | None
    llm_provider: str | None
    llm_model: str | None
    prompt_version: str | None
    created_at: datetime


@router.get("/schema")
def macro_schema() -> dict[str, Any]:
    """Expose the unchanged legacy Stage 1 contract."""
    return MacroCycleResult.model_json_schema()


@router.get("/explanations/schema")
def macro_explanation_schema() -> dict[str, Any]:
    """Expose the new versioned snapshot schema without changing the legacy schema."""
    return MacroExplanationSnapshot.model_json_schema()


@router.get("/explanations/latest", response_model=PersistedExplanationResponse)
def latest_macro_explanation(session: SessionDependency) -> PersistedExplanationResponse:
    """Read the latest persisted report; never build or invoke an LLM."""
    report = get_latest_report_by_type(session, EXPLANATION_REPORT_TYPE)
    if report is None:
        raise APIError(
            status_code=404,
            code="macro_explanation_not_found",
            message="No persisted macro explanation is available",
        )
    return PersistedExplanationResponse.model_validate(report)


@router.get("/explanations/{run_id}", response_model=PersistedExplanationResponse)
def macro_explanation_detail(
    run_id: UUID,
    session: SessionDependency,
) -> PersistedExplanationResponse:
    """Read one persisted report by run ID with zero external side effects."""
    report = get_report_for_run_type(session, run_id, EXPLANATION_REPORT_TYPE)
    if report is None:
        raise APIError(
            status_code=404,
            code="macro_explanation_not_found",
            message="Macro explanation was not found",
        )
    return PersistedExplanationResponse.model_validate(report)
