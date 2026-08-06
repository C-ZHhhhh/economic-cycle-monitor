"""Liveness and database readiness routes."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api.errors import APIError
from app.database.session import check_database

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Simple process liveness response."""

    status: Literal["ok"]
    service: str
    version: str


class ReadyResponse(BaseModel):
    """Database and configuration readiness response."""

    status: Literal["ready"]
    database: Literal["ok"]


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Report process liveness without touching PostgreSQL."""
    return HealthResponse(
        status="ok", service=request.app.state.settings.app_name, version=__version__
    )


@router.get("/ready", response_model=ReadyResponse)
def ready(request: Request) -> ReadyResponse:
    """Report readiness after a minimal PostgreSQL round trip."""
    try:
        check_database(request.app.state.engine)
    except (SQLAlchemyError, OSError) as exc:
        raise APIError(
            status_code=503,
            code="not_ready",
            message="Database is unavailable",
        ) from exc
    return ReadyResponse(status="ready", database="ok")
