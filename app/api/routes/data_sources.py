"""Safe FRED data-source status route."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_request_session
from app.database.models import DataSourceRequest, IndicatorRegistry

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_request_session)]


def _request_summary(item: DataSourceRequest | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "endpoint": item.endpoint,
        "series_id": item.series_id,
        "finished_at": item.finished_at,
        "http_status": item.http_status,
        "error_type": item.error_type,
        "from_cache": item.from_cache,
    }


@router.get("/fred/status")
def fred_status(request: Request, session: SessionDependency) -> dict[str, Any]:
    """Report readiness and activity, revealing only whether a key exists."""
    latest_success = session.scalar(
        select(DataSourceRequest)
        .where(DataSourceRequest.source == "fred", DataSourceRequest.success.is_(True))
        .order_by(DataSourceRequest.finished_at.desc())
        .limit(1)
    )
    latest_failure = session.scalar(
        select(DataSourceRequest)
        .where(DataSourceRequest.source == "fred", DataSourceRequest.success.is_(False))
        .order_by(DataSourceRequest.finished_at.desc())
        .limit(1)
    )
    registered = session.scalar(select(func.count()).select_from(IndicatorRegistry)) or 0
    validated = (
        session.scalar(
            select(func.count())
            .select_from(IndicatorRegistry)
            .where(IndicatorRegistry.metadata_status.in_({"valid", "valid_with_warning"}))
        )
        or 0
    )
    settings = request.app.state.settings
    return {
        "configured": settings.fred_api_key is not None,
        "api_key_configured": settings.fred_api_key is not None,
        "base_url": str(settings.fred_base_url),
        "cache": {
            "enabled": settings.fred_cache_enabled,
            "ttl_seconds": settings.fred_cache_ttl_seconds,
        },
        "registered_indicator_count": registered,
        "validated_indicator_count": validated,
        "latest_success": _request_summary(latest_success),
        "latest_failure": _request_summary(latest_failure),
    }
