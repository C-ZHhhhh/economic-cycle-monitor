"""Persistence and bounded queries for official-source mappings."""

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.database.models import IndicatorSourceMapping
from app.source_mappings.policy import source_mapping_is_direct
from app.source_mappings.schemas import (
    SourceMappingCatalog,
    SourceMappingDefinition,
    SourceMappingSeedResult,
)
from app.source_registry.config import load_source_registry, storage_source_name
from app.source_registry.schemas import SourceRegistryCatalog, SourceRegistryPolicy


def _revision_lookback_years(policy: SourceRegistryPolicy) -> int:
    """Materialize the widest configured reconciliation window for legacy services."""
    layers = (
        policy.revision.recent_reconciliation,
        policy.revision.annual_or_benchmark_reconciliation,
        policy.revision.deep_reconciliation,
    )
    years = [item.window_years for item in layers if item.window_years is not None]
    if years:
        return max(years)
    months = [item.window_months for item in layers if item.window_months is not None]
    if months:
        return max(1, (max(months) + 11) // 12)
    days = [item.window_days for item in layers if item.window_days is not None]
    if days:
        return max(1, (max(days) + 364) // 365)
    return 100 if policy.primary_source.value == "FEDERAL_RESERVE" else 1


def _configured_values(
    definition: SourceMappingDefinition, policy: SourceRegistryPolicy
) -> dict[str, object]:
    is_direct = source_mapping_is_direct(definition.source_parameters)
    return {
        "source_dataset": definition.source_dataset,
        "source_parameters_json": definition.source_parameters,
        "source_name": definition.official_name,
        "source_unit": definition.source_unit,
        "source_frequency": definition.expected_frequency,
        "source_seasonal_adjustment": definition.expected_seasonal_adjustment,
        "source_priority": 1 if is_direct else 2,
        "fallback_source": (
            storage_source_name(policy.fallback_source.value)
            if policy.fallback_source is not None
            else None
        ),
        "vintage_source": "alfred",
        "normalization_rule": definition.normalization_rule,
        "comparison_tolerance": Decimal(policy.comparison.absolute_tolerance),
        "distribution_lag_days": policy.comparison.distribution_lag_policy.allowed_days,
        "revision_lookback_years": _revision_lookback_years(policy),
        "enabled": definition.enabled,
    }


def seed_source_mappings(
    session: Session,
    catalog: SourceMappingCatalog,
    registry: SourceRegistryCatalog | None = None,
) -> SourceMappingSeedResult:
    """Synchronize reviewed mappings without deleting older mapping versions."""
    source_registry = registry or load_source_registry()
    policies = source_registry.by_indicator()
    existing = {
        (item.indicator_id, item.source, item.source_series_id, item.mapping_version): item
        for item in session.scalars(
            select(IndicatorSourceMapping).where(IndicatorSourceMapping.source == catalog.source)
        ).all()
    }
    result = SourceMappingSeedResult()
    for definition in catalog.mappings:
        try:
            policy = policies[definition.indicator_id]
        except KeyError as error:
            raise ValueError(
                f"Mapping {definition.indicator_id} has no source registry policy"
            ) from error
        key = (
            definition.indicator_id,
            definition.source,
            definition.source_series_id,
            definition.mapping_version,
        )
        values = _configured_values(definition, policy)
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
            config_can_supersede_static_warning = (
                record.mapping_status == "valid_with_warning"
                and definition.mapping_status == "valid"
                and not record.mapping_warnings_json
            )
            if record.mapping_status != definition.mapping_status and (
                record.metadata_checked_at is None or config_can_supersede_static_warning
            ):
                record.mapping_status = definition.mapping_status
                changed = True
            if changed:
                result.updated += 1
            else:
                result.unchanged += 1
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
