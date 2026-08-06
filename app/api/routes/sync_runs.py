"""Bounded sync-run audit routes."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_request_session
from app.api.errors import APIError
from app.database.models import DataSourceRequest, SyncRun

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_request_session)]


def _run_payload(item: SyncRun) -> dict[str, Any]:
    return {
        "run_id": item.run_id,
        "operation": item.operation,
        "status": item.status,
        "started_at": item.started_at,
        "finished_at": item.finished_at,
        "parameters": item.parameters_json,
        "indicator_count": item.indicator_count,
        "success_count": item.success_count,
        "failure_count": item.failure_count,
        "skipped_count": item.skipped_count,
        "inserted_count": item.inserted_count,
        "revised_count": item.revised_count,
        "quality_event_count": item.quality_event_count,
        "request_count": item.request_count,
        "cache_hit_count": item.cache_hit_count,
        "errors": item.error_summary_json,
    }


@router.get("")
def sync_runs_index(
    session: SessionDependency,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, Any]]:
    """List recent pipeline runs."""
    rows = session.scalars(
        select(SyncRun).order_by(SyncRun.started_at.desc()).offset(offset).limit(limit)
    ).all()
    return [_run_payload(item) for item in rows]


@router.get("/{run_id}")
def sync_run_detail(run_id: UUID, session: SessionDependency) -> dict[str, Any]:
    """Return one run and sanitized request audit records."""
    item = session.get(SyncRun, run_id)
    if item is None:
        raise APIError(status_code=404, code="sync_run_not_found", message="Sync run was not found")
    requests = session.scalars(
        select(DataSourceRequest)
        .where(DataSourceRequest.sync_run_id == run_id)
        .order_by(DataSourceRequest.started_at)
        .limit(1000)
    ).all()
    return {
        **_run_payload(item),
        "requests": [
            {
                "source": request.source,
                "endpoint": request.endpoint,
                "series_id": request.series_id,
                "parameters": request.request_parameters_json,
                "started_at": request.started_at,
                "finished_at": request.finished_at,
                "http_status": request.http_status,
                "success": request.success,
                "attempt_count": request.attempt_count,
                "response_hash": request.response_hash,
                "from_cache": request.from_cache,
                "error_type": request.error_type,
                "error_message": request.error_message,
            }
            for request in requests
        ],
    }
