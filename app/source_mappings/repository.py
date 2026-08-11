"""Persistence and bounded queries for official-source mappings."""

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.database.models import IndicatorRegistry, IndicatorSourceMapping
from app.source_mappings.policy import source_mapping_is_direct
from app.source_mappings.schemas import (
    SourceMappingCatalog,
    SourceMappingDefinition,
    SourceMappingSeedResult,
)


def _configured_values(definition: SourceMappingDefinition) -> dict[str, object]:
    return {
        "source_dataset": definition.source_dataset,
        "source_parameters_json": definition.source_parameters,
        "source_name": definition.official_name,
        "source_unit": definition.source_unit,
        "source_frequency": definition.expected_frequency,
        "source_seasonal_adjustment": definition.expected_seasonal_adjustment,
        "source_priority": definition.source_priority,
        "fallback_source": definition.fallback_source,
        "vintage_source": definition.vintage_source,
        "normalization_rule": definition.normalization_rule,
        "comparison_tolerance": Decimal(definition.comparison_tolerance),
        "distribution_lag_days": definition.distribution_lag_days,
        "revision_lookback_years": definition.revision_lookback_years,
        "enabled": definition.enabled,
    }


def seed_source_mappings(
    session: Session, catalog: SourceMappingCatalog
) -> SourceMappingSeedResult:
    """Synchronize reviewed mappings without deleting older mapping versions."""
    existing = {
        (item.indicator_id, item.source, item.source_series_id, item.mapping_version): item
        for item in session.scalars(
            select(IndicatorSourceMapping).where(IndicatorSourceMapping.source == catalog.source)
        ).all()
    }
    registry = {
        item.indicator_id: item
        for item in session.scalars(
            select(IndicatorRegistry).where(
                IndicatorRegistry.indicator_id.in_(
                    [definition.indicator_id for definition in catalog.mappings]
                )
            )
        ).all()
    }
    result = SourceMappingSeedResult()
    for definition in catalog.mappings:
        key = (
            definition.indicator_id,
            definition.source,
            definition.source_series_id,
            definition.mapping_version,
        )
        values = _configured_values(definition)
        record = existing.get(key)
        if record is None:
            session.add(
                IndicatorSourceMapping(
                    indicator_id=definition.indicator_id,
                    source=definition.source,
                    source_series_id=definition.source_series_id,
                    mapping_version=definition.mapping_version,
                    mapping_status=definition.mapping_status,
                    mapping_warnings_json=[],
                    **values,
                )
            )
            result.inserted += 1
        else:
            changed = False
            for field_name, value in values.items():
                if getattr(record, field_name) != value:
                    setattr(record, field_name, value)
                    changed = True
            if (
                record.metadata_checked_at is None
                and record.mapping_status != definition.mapping_status
            ):
                record.mapping_status = definition.mapping_status
                changed = True
            if changed:
                result.updated += 1
            else:
                result.unchanged += 1
        indicator = registry.get(definition.indicator_id)
        if (
            indicator is not None
            and definition.enabled
            and definition.mapping_status in {"valid", "valid_with_warning"}
            and definition.source_priority == 1
            and source_mapping_is_direct(definition.source_parameters)
        ):
            indicator.primary_source = definition.source
            indicator.primary_source_series_id = definition.source_series_id
            indicator.fallback_source = definition.fallback_source
    configured_keys = {
        (
            item.indicator_id,
            item.source,
            item.source_series_id,
            item.mapping_version,
        )
        for item in catalog.mappings
    }
    for key, record in existing.items():
        if key not in configured_keys and record.enabled:
            record.enabled = False
            result.disabled += 1
    session.flush()
    return result


def list_source_mappings(
    session: Session,
    *,
    source: str | None = None,
    indicator_id: str | None = None,
    enabled: bool | None = None,
    offset: int = 0,
    limit: int = 100,
) -> Sequence[IndicatorSourceMapping]:
    """Return a bounded mapping page."""
    statement: Select[tuple[IndicatorSourceMapping]] = select(IndicatorSourceMapping)
    if source is not None:
        statement = statement.where(IndicatorSourceMapping.source == source)
    if indicator_id is not None:
        statement = statement.where(IndicatorSourceMapping.indicator_id == indicator_id)
    if enabled is not None:
        statement = statement.where(IndicatorSourceMapping.enabled == enabled)
    return session.scalars(
        statement.order_by(
            IndicatorSourceMapping.indicator_id,
            IndicatorSourceMapping.source_priority,
        )
        .offset(offset)
        .limit(limit)
    ).all()


def get_active_source_mapping(
    session: Session, indicator_id: str, source: str
) -> IndicatorSourceMapping | None:
    """Return the highest-priority active usable mapping for an indicator/source."""
    candidates = session.scalars(
        select(IndicatorSourceMapping)
        .where(
            IndicatorSourceMapping.indicator_id == indicator_id,
            IndicatorSourceMapping.source == source,
            IndicatorSourceMapping.enabled.is_(True),
            IndicatorSourceMapping.mapping_status.in_({"valid", "valid_with_warning"}),
        )
        .order_by(IndicatorSourceMapping.source_priority, IndicatorSourceMapping.id.desc())
        .limit(100)
    ).all()
    return next(
        (item for item in candidates if source_mapping_is_direct(item.source_parameters_json)),
        None,
    )
