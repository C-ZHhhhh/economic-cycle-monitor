"""Repository operations for the indicator registry."""

from collections.abc import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.database.models import IndicatorRegistry


def get_indicator(session: Session, indicator_id: str) -> IndicatorRegistry | None:
    """Return one indicator by its stable business key."""
    statement = select(IndicatorRegistry).where(IndicatorRegistry.indicator_id == indicator_id)
    return session.scalar(statement)


def list_indicators(
    session: Session,
    *,
    module: str | None = None,
    enabled: bool | None = None,
    metadata_status: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> Sequence[IndicatorRegistry]:
    """List indicators with bounded filtering."""
    statement: Select[tuple[IndicatorRegistry]] = select(IndicatorRegistry)
    if module is not None:
        statement = statement.where(IndicatorRegistry.module == module)
    if enabled is not None:
        statement = statement.where(IndicatorRegistry.enabled.is_(enabled))
    if metadata_status is not None:
        statement = statement.where(IndicatorRegistry.metadata_status == metadata_status)
    statement = statement.order_by(IndicatorRegistry.indicator_id).offset(offset).limit(limit)
    return session.scalars(statement).all()
