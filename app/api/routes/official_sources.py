"""Bounded official-source mapping and comparison query routes."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_request_session
from app.api.errors import APIError
from app.data_pipeline.source_comparison import list_source_comparisons
from app.source_mappings.repository import list_source_mappings

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_request_session)]


def _mapping_payload(item: Any) -> dict[str, Any]:
    return {
        "indicator_id": item.indicator_id,
        "source": item.source,
        "source_series_id": item.source_series_id,
        "source_dataset": item.source_dataset,
        "source_parameters": item.source_parameters_json,
        "source_name": item.source_name,
        "source_unit": item.source_unit,
        "source_frequency": item.source_frequency,
        "source_seasonal_adjustment": item.source_seasonal_adjustment,
        "source_priority": item.source_priority,
        "fallback_source": item.fallback_source,
        "vintage_source": item.vintage_source,
        "normalization_rule": item.normalization_rule,
        "comparison_tolerance": item.comparison_tolerance,
        "mapping_status": item.mapping_status,
        "mapping_warnings": item.mapping_warnings_json,
        "enabled": item.enabled,
        "mapping_version": item.mapping_version,
        "metadata_checked_at": item.metadata_checked_at,
    }


@router.get("/source-mappings")
def source_mappings(
    session: SessionDependency,
    source: str | None = None,
    enabled: bool | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, Any]]:
    """List a bounded page of reviewed official-source mappings."""
    return [
        _mapping_payload(item)
        for item in list_source_mappings(
            session,
            source=source.lower() if source else None,
            enabled=enabled,
            offset=offset,
            limit=limit,
        )
    ]


@router.get("/source-mappings/{indicator_id}")
def source_mapping_detail(indicator_id: str, session: SessionDependency) -> list[dict[str, Any]]:
    """Return reviewed mappings for one stable indicator business key."""
    rows = list_source_mappings(session, indicator_id=indicator_id, limit=100)
    if not rows:
        raise APIError(
            status_code=404,
            code="source_mapping_not_found",
            message="Source mapping was not found",
        )
    return [_mapping_payload(item) for item in rows]


def _comparison_payload(item: Any) -> dict[str, Any]:
    return {
        "run_id": item.run_id,
        "indicator_id": item.indicator_id,
        "observation_date": item.observation_date,
        "primary_source": item.primary_source,
        "secondary_source": item.secondary_source,
        "primary_value": item.primary_value,
        "secondary_value": item.secondary_value,
        "normalized_primary_value": item.normalized_primary_value,
        "normalized_secondary_value": item.normalized_secondary_value,
        "absolute_difference": item.absolute_difference,
        "relative_difference": item.relative_difference,
        "tolerance": item.tolerance,
        "comparison_status": item.comparison_status,
        "details": item.details_json,
        "created_at": item.created_at,
    }


@router.get("/source-comparisons")
def source_comparisons(
    session: SessionDependency,
    status: str | None = None,
    primary_source: str | None = None,
    secondary_source: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, Any]]:
    """List a bounded newest-first comparison result page."""
    return [
        _comparison_payload(item)
        for item in list_source_comparisons(
            session,
            comparison_status=status,
            primary_source=primary_source.lower() if primary_source else None,
            secondary_source=secondary_source.lower() if secondary_source else None,
            offset=offset,
            limit=limit,
        )
    ]


@router.get("/source-comparisons/{indicator_id}")
def source_comparison_detail(
    indicator_id: str,
    session: SessionDependency,
    status: str | None = None,
    primary_source: str | None = None,
    secondary_source: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, Any]]:
    """List a bounded comparison page for one indicator."""
    return [
        _comparison_payload(item)
        for item in list_source_comparisons(
            session,
            indicator_id=indicator_id,
            comparison_status=status,
            primary_source=primary_source.lower() if primary_source else None,
            secondary_source=secondary_source.lower() if secondary_source else None,
            offset=offset,
            limit=limit,
        )
    ]
