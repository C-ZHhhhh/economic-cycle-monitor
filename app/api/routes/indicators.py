"""Bounded indicator registry and observation query routes."""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_request_session
from app.api.errors import APIError
from app.data_quality.service import quality_summary_for_indicator
from app.database.models import ObservationVintage, RawObservation
from app.indicators.repository import get_indicator, list_indicators

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_request_session)]


def _registry_payload(item: Any) -> dict[str, Any]:
    return {
        "indicator_id": item.indicator_id,
        "name": item.name,
        "description": item.description,
        "module": item.module,
        "owner_agent": item.owner_agent,
        "fred_series_id": item.fred_series_id,
        "observation_frequency": item.observation_frequency,
        "release_frequency": item.release_frequency,
        "unit": item.unit,
        "seasonal_adjustment": item.seasonal_adjustment,
        "max_staleness_days": item.max_staleness_days,
        "enabled": item.enabled,
        "config_version": item.config_version,
        "metadata_status": item.metadata_status,
        "metadata_checked_at": item.metadata_checked_at,
        "source_metadata": {
            "title": item.source_title,
            "notes": item.source_notes,
            "frequency": item.source_frequency,
            "units": item.source_units,
            "seasonal_adjustment": item.source_seasonal_adjustment,
            "observation_start": item.source_observation_start,
            "observation_end": item.source_observation_end,
            "last_updated": item.source_last_updated,
            "popularity": item.source_popularity,
            "release": item.source_release_json,
        },
        "metadata_warnings": item.metadata_warnings_json,
    }


@router.get("")
def indicators_index(
    session: SessionDependency,
    module: str | None = None,
    enabled: bool | None = None,
    metadata_status: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, Any]]:
    """List configured indicators with bounded filters."""
    return [
        _registry_payload(item)
        for item in list_indicators(
            session,
            module=module,
            enabled=enabled,
            metadata_status=metadata_status,
            offset=offset,
            limit=limit,
        )
    ]


@router.get("/{indicator_id}")
def indicator_detail(indicator_id: str, session: SessionDependency) -> dict[str, Any]:
    """Return registry, latest observation, sync time, and quality summary."""
    item = get_indicator(session, indicator_id)
    if item is None:
        raise APIError(
            status_code=404, code="indicator_not_found", message="Indicator was not found"
        )
    latest = session.scalar(
        select(RawObservation)
        .where(RawObservation.indicator_id == indicator_id)
        .order_by(RawObservation.observation_date.desc())
        .limit(1)
    )
    payload = _registry_payload(item)
    payload["latest_observation"] = (
        {
            "date": latest.observation_date,
            "value": latest.value,
            "source": latest.source,
            "retrieved_at": latest.retrieved_at,
        }
        if latest
        else None
    )
    payload["last_sync_time"] = session.scalar(
        select(func.max(RawObservation.retrieved_at)).where(
            RawObservation.indicator_id == indicator_id
        )
    )
    payload["data_quality"] = quality_summary_for_indicator(session, indicator_id)
    return payload


@router.get("/{indicator_id}/observations")
def indicator_observations(
    indicator_id: str,
    session: SessionDependency,
    start: date | None = None,
    end: date | None = None,
    current_only: bool = True,
    vintage_date: date | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """Return a bounded current or point-in-time observation page."""
    if get_indicator(session, indicator_id) is None:
        raise APIError(
            status_code=404, code="indicator_not_found", message="Indicator was not found"
        )
    if vintage_date is not None or not current_only:
        statement = select(ObservationVintage).where(
            ObservationVintage.indicator_id == indicator_id
        )
        if vintage_date is not None:
            statement = statement.where(ObservationVintage.vintage_date == vintage_date)
        if start is not None:
            statement = statement.where(ObservationVintage.observation_date >= start)
        if end is not None:
            statement = statement.where(ObservationVintage.observation_date <= end)
        rows = session.scalars(
            statement.order_by(ObservationVintage.observation_date).offset(offset).limit(limit)
        ).all()
        return [
            {
                "date": row.observation_date,
                "value": row.current_value,
                "source": row.source,
                "vintage_date": row.vintage_date,
                "revision_number": row.revision_number,
            }
            for row in rows
        ]
    current_statement = select(RawObservation).where(RawObservation.indicator_id == indicator_id)
    if start is not None:
        current_statement = current_statement.where(RawObservation.observation_date >= start)
    if end is not None:
        current_statement = current_statement.where(RawObservation.observation_date <= end)
    current_rows = session.scalars(
        current_statement.order_by(RawObservation.observation_date).offset(offset).limit(limit)
    ).all()
    return [
        {
            "date": row.observation_date,
            "value": row.value,
            "source": row.source,
            "vintage_date": row.source_vintage_date,
            "retrieved_at": row.retrieved_at,
        }
        for row in current_rows
    ]
