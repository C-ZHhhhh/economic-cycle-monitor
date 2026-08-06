"""Idempotent indicator registry synchronization."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import IndicatorRegistry
from app.indicators.schemas import IndicatorCatalog, IndicatorDefinition, SeedResult


def _configured_values(definition: IndicatorDefinition) -> dict[str, object]:
    return {
        "name": definition.name,
        "description": definition.description,
        "module": definition.module,
        "owner_agent": definition.owner_agent,
        "fred_series_id": definition.fred_series_id,
        "primary_source": definition.primary_source,
        "primary_source_series_id": definition.primary_source_series_id,
        "fallback_source": definition.fallback_source,
        "observation_frequency": definition.observation_frequency,
        "release_frequency": definition.release_frequency,
        "unit": definition.expected_unit,
        "seasonal_adjustment": definition.expected_seasonal_adjustment,
        "direction": definition.direction,
        "weight": Decimal(definition.weight),
        "max_staleness_days": definition.max_staleness_days,
        "revision_policy": definition.revision_policy,
        "enabled": definition.enabled,
        "config_version": definition.config_version,
    }


def seed_indicator_catalog(session: Session, catalog: IndicatorCatalog) -> SeedResult:
    """Insert or update configured indicators without deleting historical rows."""
    existing = {
        item.indicator_id: item for item in session.scalars(select(IndicatorRegistry)).all()
    }
    result = SeedResult()
    for definition in catalog.indicators:
        values = _configured_values(definition)
        record = existing.get(definition.indicator_id)
        if record is None:
            session.add(IndicatorRegistry(indicator_id=definition.indicator_id, **values))
            result.inserted += 1
            if not definition.enabled:
                result.disabled += 1
            continue
        changed = False
        for field_name, value in values.items():
            if getattr(record, field_name) != value:
                setattr(record, field_name, value)
                changed = True
        if changed:
            result.updated += 1
        else:
            result.unchanged += 1
        if not definition.enabled:
            result.disabled += 1
    session.flush()
    return result
