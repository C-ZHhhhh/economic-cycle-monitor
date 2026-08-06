"""Bounded data-quality event query route."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_request_session
from app.data_quality.repository import list_quality_events

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_request_session)]


@router.get("")
def data_quality_index(
    session: SessionDependency,
    indicator_id: str | None = None,
    severity: str | None = None,
    event_type: str | None = None,
    unresolved_only: bool = False,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List quality findings without exposing raw provider payloads."""
    return [
        {
            "id": item.id,
            "run_id": item.run_id,
            "indicator_id": item.indicator_id,
            "event_type": item.event_type,
            "severity": item.severity,
            "message": item.message,
            "details": item.details_json,
            "detected_at": item.detected_at,
            "resolved_at": item.resolved_at,
            "resolution_notes": item.resolution_notes,
        }
        for item in list_quality_events(
            session,
            indicator_id=indicator_id,
            severity=severity,
            event_type=event_type,
            unresolved_only=unresolved_only,
            offset=offset,
            limit=limit,
        )
    ]
