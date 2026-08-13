"""Idempotent indicator registry synchronization."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import IndicatorRegistry
from app.indicators.schemas import IndicatorCatalog, IndicatorDefinition, SeedResult
from app.source_registry.config import load_source_registry, storage_source_name
from app.source_registry.schemas import SourceRegistryCatalog, SourceRegistryPolicy


def _configured_values(
    definition: IndicatorDefinition, policy: SourceRegistryPolicy
) -> dict[str, object]:
    fallback_source = (
        storage_source_name(policy.fallback_source.value)
        if policy.fallback_source is not None
        else None
    )
    return {
        "name": definition.name,
        "description": definition.description,
        "module": definition.module,
        "owner_agent": definition.owner_agent,
        "fred_series_id": definition.fred_series_id,
        "primary_source": storage_source_name(policy.primary_source.value),
        "primary_source_series_id": policy.primary_source_identity,
        "fallback_source": fallback_source,
        "observation_frequency": definition.observation_frequency,
        "release_frequency": definition.observation_frequency,
        "unit": definition.expected_unit,
        "seasonal_adjustment": definition.expected_seasonal_adjustment,
        "direction": definition.direction,
        "weight": Decimal(definition.weight),
        "max_staleness_days": (
            policy.freshness.legacy_ceiling_days
            or policy.freshness.expected_lag_days + policy.freshness.grace_days
        ),
        "revision_policy": f"registry:{policy.registry_policy_version}",
        "enabled": definition.enabled,
        "config_version": definition.config_version,
    }


def seed_indicator_catalog(
    session: Session,
    catalog: IndicatorCatalog,
    registry: SourceRegistryCatalog | None = None,
) -> SeedResult:
    """Insert or update configured indicators without deleting historical rows."""
    source_registry = registry or load_source_registry()
    policies = source_registry.by_indicator()
    configured_ids = {item.indicator_id for item in catalog.indicators}
    if configured_ids != set(policies):
        raise ValueError("Indicator catalog and source registry sets must match exactly")
    existing = {
        item.indicator_id: item for item in session.scalars(select(IndicatorRegistry)).all()
    }
    result = SeedResult()
    for definition in catalog.indicators:
        values = _configured_values(definition, policies[definition.indicator_id])
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
