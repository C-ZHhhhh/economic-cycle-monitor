"""Safe FRED data-source status route."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_request_session
from app.database.models import DataSourceRequest, IndicatorRegistry, IndicatorSourceMapping

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


@router.get("/bls/status")
def bls_status(request: Request, session: SessionDependency) -> dict[str, Any]:
    """Report BLS configuration, mapping counts, and secret-free activity."""
    latest_success = session.scalar(
        select(DataSourceRequest)
        .where(DataSourceRequest.source == "bls", DataSourceRequest.success.is_(True))
        .order_by(DataSourceRequest.finished_at.desc())
        .limit(1)
    )
    latest_failure = session.scalar(
        select(DataSourceRequest)
        .where(DataSourceRequest.source == "bls", DataSourceRequest.success.is_(False))
        .order_by(DataSourceRequest.finished_at.desc())
        .limit(1)
    )
    mapping_count = (
        session.scalar(
            select(func.count())
            .select_from(IndicatorSourceMapping)
            .where(IndicatorSourceMapping.source == "bls", IndicatorSourceMapping.enabled.is_(True))
        )
        or 0
    )
    usable_count = (
        session.scalar(
            select(func.count())
            .select_from(IndicatorSourceMapping)
            .where(
                IndicatorSourceMapping.source == "bls",
                IndicatorSourceMapping.enabled.is_(True),
                IndicatorSourceMapping.mapping_status.in_({"valid", "valid_with_warning"}),
            )
        )
        or 0
    )
    settings = request.app.state.settings
    return {
        "enabled": settings.bls_enabled,
        "configured": settings.bls_api_key is not None,
        "api_key_configured": settings.bls_api_key is not None,
        "base_url": str(settings.bls_base_url),
        "cache": {
            "enabled": settings.bls_cache_enabled,
            "ttl_seconds": settings.bls_cache_ttl_seconds,
        },
        "registered_mapping_count": mapping_count,
        "usable_mapping_count": usable_count,
        "latest_success": _request_summary(latest_success),
        "latest_failure": _request_summary(latest_failure),
    }


@router.get("/bea/status")
def bea_status(request: Request, session: SessionDependency) -> dict[str, Any]:
    """Report BEA configuration, reviewed mappings, and secret-free activity."""
    latest_success = session.scalar(
        select(DataSourceRequest)
        .where(DataSourceRequest.source == "bea", DataSourceRequest.success.is_(True))
        .order_by(DataSourceRequest.finished_at.desc())
        .limit(1)
    )
    latest_failure = session.scalar(
        select(DataSourceRequest)
        .where(DataSourceRequest.source == "bea", DataSourceRequest.success.is_(False))
        .order_by(DataSourceRequest.finished_at.desc())
        .limit(1)
    )
    mappings = (
        session.scalar(
            select(func.count())
            .select_from(IndicatorSourceMapping)
            .where(IndicatorSourceMapping.source == "bea", IndicatorSourceMapping.enabled.is_(True))
        )
        or 0
    )
    pending = (
        session.scalar(
            select(func.count())
            .select_from(IndicatorSourceMapping)
            .where(
                IndicatorSourceMapping.source == "bea",
                IndicatorSourceMapping.enabled.is_(True),
                IndicatorSourceMapping.mapping_status == "pending_review",
            )
        )
        or 0
    )
    usable = (
        session.scalar(
            select(func.count())
            .select_from(IndicatorSourceMapping)
            .where(
                IndicatorSourceMapping.source == "bea",
                IndicatorSourceMapping.enabled.is_(True),
                IndicatorSourceMapping.mapping_status.in_({"valid", "valid_with_warning"}),
            )
        )
        or 0
    )
    settings = request.app.state.settings
    return {
        "enabled": settings.bea_enabled,
        "configured": settings.bea_api_key is not None,
        "api_key_configured": settings.bea_api_key is not None,
        "base_url": str(settings.bea_base_url),
        "cache": {
            "enabled": settings.bea_cache_enabled,
            "ttl_seconds": settings.bea_cache_ttl_seconds,
        },
        "registered_mapping_count": mappings,
        "usable_mapping_count": usable,
        "pending_review_count": pending,
        "latest_success": _request_summary(latest_success),
        "latest_failure": _request_summary(latest_failure),
    }


@router.get("/census/status")
def census_status(request: Request, session: SessionDependency) -> dict[str, Any]:
    """Report Census configuration and candidate roles without exposing the API key."""
    latest_success = session.scalar(
        select(DataSourceRequest)
        .where(DataSourceRequest.source == "census", DataSourceRequest.success.is_(True))
        .order_by(DataSourceRequest.finished_at.desc())
        .limit(1)
    )
    latest_failure = session.scalar(
        select(DataSourceRequest)
        .where(DataSourceRequest.source == "census", DataSourceRequest.success.is_(False))
        .order_by(DataSourceRequest.finished_at.desc())
        .limit(1)
    )
    rows = session.scalars(
        select(IndicatorSourceMapping).where(
            IndicatorSourceMapping.source == "census",
            IndicatorSourceMapping.enabled.is_(True),
        )
    ).all()
    direct = [
        item
        for item in rows
        if item.source_parameters_json.get("mapping_type", "DIRECT") == "DIRECT"
    ]
    supporting = [
        item
        for item in rows
        if item.source_parameters_json.get("mapping_type") == "SUPPORTING_COMPONENT"
    ]
    usable_direct = [
        item for item in direct if item.mapping_status in {"valid", "valid_with_warning"}
    ]
    settings = request.app.state.settings
    return {
        "enabled": settings.census_enabled,
        "configured": settings.census_api_key is not None,
        "api_key_configured": settings.census_api_key is not None,
        "base_url": str(settings.census_base_url),
        "cache": {
            "enabled": settings.census_cache_enabled,
            "ttl_seconds": settings.census_cache_ttl_seconds,
        },
        "registered_mapping_count": len(rows),
        "direct_mapping_count": len(direct),
        "usable_direct_mapping_count": len(usable_direct),
        "supporting_component_count": len(supporting),
        "pending_review_count": sum(item.mapping_status == "pending_review" for item in rows),
        "latest_success": _request_summary(latest_success),
        "latest_failure": _request_summary(latest_failure),
    }


def _public_source_status(
    session: Session,
    *,
    source: str,
    enabled: bool,
    base_url: str,
    cache_enabled: bool,
    cache_ttl_seconds: int,
) -> dict[str, Any]:
    """Build a shared credential-free status for public official file sources."""
    latest_success = session.scalar(
        select(DataSourceRequest)
        .where(DataSourceRequest.source == source, DataSourceRequest.success.is_(True))
        .order_by(DataSourceRequest.finished_at.desc())
        .limit(1)
    )
    latest_failure = session.scalar(
        select(DataSourceRequest)
        .where(DataSourceRequest.source == source, DataSourceRequest.success.is_(False))
        .order_by(DataSourceRequest.finished_at.desc())
        .limit(1)
    )
    rows = session.scalars(
        select(IndicatorSourceMapping).where(
            IndicatorSourceMapping.source == source,
            IndicatorSourceMapping.enabled.is_(True),
        )
    ).all()
    return {
        "enabled": enabled,
        "configured": True,
        "credentials_required": False,
        "base_url": base_url,
        "cache": {"enabled": cache_enabled, "ttl_seconds": cache_ttl_seconds},
        "registered_mapping_count": len(rows),
        "usable_mapping_count": sum(
            item.mapping_status in {"valid", "valid_with_warning"} for item in rows
        ),
        "pending_review_count": sum(item.mapping_status == "pending_review" for item in rows),
        "latest_success": _request_summary(latest_success),
        "latest_failure": _request_summary(latest_failure),
    }


@router.get("/federal-reserve/status")
def federal_reserve_status(request: Request, session: SessionDependency) -> dict[str, Any]:
    """Report public Board release configuration and reviewed mapping counts."""
    settings = request.app.state.settings
    return _public_source_status(
        session,
        source="federal_reserve",
        enabled=settings.federal_reserve_enabled,
        base_url=str(settings.federal_reserve_base_url),
        cache_enabled=settings.federal_reserve_cache_enabled,
        cache_ttl_seconds=settings.federal_reserve_cache_ttl_seconds,
    )


@router.get("/dol/status")
def dol_status(request: Request, session: SessionDependency) -> dict[str, Any]:
    """Report public DOL Weekly Claims configuration and mapping counts."""
    settings = request.app.state.settings
    return _public_source_status(
        session,
        source="dol",
        enabled=settings.dol_enabled,
        base_url=str(settings.dol_base_url),
        cache_enabled=settings.dol_cache_enabled,
        cache_ttl_seconds=settings.dol_cache_ttl_seconds,
    )
