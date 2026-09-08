"""Independent current-data and point-in-time readiness gates."""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_quality.repository import list_quality_events
from app.data_quality.scope import RuntimeDomain
from app.database.models import IndicatorRegistry
from app.indicators.config import load_indicator_catalog
from app.source_mappings.config import SOURCE_MAPPING_PATHS, load_source_mapping_catalog_for
from app.source_registry.audit import audit_materialized_policy, audit_source_registry
from app.source_registry.config import load_source_registry
from app.source_registry.current_freshness import (
    CurrentFreshnessCatalog,
    load_current_freshness_catalog,
)
from app.source_registry.health import (
    historical_audit_event_is_current,
    indicator_source_health,
    provider_health,
)
from app.source_registry.materialization import load_pit_materialization_catalog
from app.source_registry.schemas import ReadinessStatus, RuntimeHealth, SourceRegistryCatalog


def validate_data_layer(
    session: Session,
    *,
    registry: SourceRegistryCatalog | None = None,
    today: date | None = None,
    evaluated_at: datetime | None = None,
    current_freshness: CurrentFreshnessCatalog | None = None,
) -> ReadinessStatus:
    """Evaluate current readiness without overstating ALFRED backtest coverage."""
    if today is not None and evaluated_at is not None:
        raise ValueError("provide today or evaluated_at, not both")
    source_registry = registry or load_source_registry()
    freshness_catalog = current_freshness or load_current_freshness_catalog(
        source_registry=source_registry
    )
    config_audit = audit_source_registry(
        source_registry,
        load_indicator_catalog(),
        [load_source_mapping_catalog_for(source) for source in SOURCE_MAPPING_PATHS],
    )
    current_blockers = [
        f"{item.code}:{item.indicator_id or 'registry'}" for item in config_audit.issues
    ]
    rows = list(session.scalars(select(IndicatorRegistry)).all())
    current_blockers.extend(
        f"{item.code}:{item.indicator_id or 'registry'}"
        for item in audit_materialized_policy(source_registry, rows)
    )

    degraded: list[str] = []
    current_freshness_warnings: list[str] = []
    for policy in source_registry.policies:
        snapshot = indicator_source_health(
            session,
            policy,
            today=today,
            evaluated_at=evaluated_at,
            current_freshness=freshness_catalog,
        )
        if snapshot.runtime_health is RuntimeHealth.DEGRADED:
            degraded.append(f"{policy.indicator_id}:{','.join(snapshot.reasons)}")
        elif snapshot.runtime_health in {
            RuntimeHealth.ERROR,
            RuntimeHealth.STALE,
            RuntimeHealth.UNAVAILABLE,
        }:
            blocker = (
                f"CURRENT_CALENDAR_COVERAGE_EXPIRED:{policy.indicator_id}"
                if "CURRENT_CALENDAR_COVERAGE_EXPIRED" in snapshot.reasons
                else f"RUNTIME_{snapshot.runtime_health.value}:{policy.indicator_id}"
            )
            current_blockers.append(blocker)
        if "CURRENT_FRESHNESS_CALENDAR_LOW_HORIZON" in snapshot.reasons:
            current_freshness_warnings.append(
                f"CURRENT_FRESHNESS_CALENDAR_LOW_HORIZON:{policy.indicator_id}"
            )

    for event in list_quality_events(session, unresolved_only=True, limit=1000):
        if event.severity not in {"error", "critical"}:
            continue
        if historical_audit_event_is_current(session, event):
            current_blockers.append(
                f"CURRENT_{event.severity.upper()}:{event.event_type}:{event.indicator_id}"
            )

    pit_contract_blockers = [
        f"PIT_COVERAGE_UNVERIFIED:{policy.indicator_id}"
        for policy in source_registry.policies
        if policy.vintage.backtest_safety == "UNVERIFIED"
    ]
    try:
        load_pit_materialization_catalog(source_registry=source_registry)
    except (OSError, ValueError) as error:
        pit_contract_blockers.append(
            f"PIT_MATERIALIZATION_AUTHORITY_INVALID:{type(error).__name__}"
        )
    alfred_health = provider_health(
        session,
        "fred",
        runtime_domain=RuntimeDomain.ALFRED_VINTAGE,
    )
    pit_runtime_blockers = (
        []
        if alfred_health.runtime_health is RuntimeHealth.HEALTHY
        else [f"PIT_RUNTIME_{alfred_health.runtime_health.value}:ALFRED_VINTAGE"]
    )
    pit_blockers = [*pit_contract_blockers, *pit_runtime_blockers]
    return ReadinessStatus(
        registry_policy_version=source_registry.registry_policy_version,
        registry_hash=source_registry.registry_hash,
        current_freshness_version=freshness_catalog.current_freshness_version,
        current_freshness_hash=freshness_catalog.current_freshness_hash,
        current_data_ready=not current_blockers,
        pit_contract_ready=not pit_contract_blockers,
        pit_runtime_ready=not pit_runtime_blockers,
        pit_runtime_health=alfred_health.runtime_health,
        pit_backtest_ready=not pit_blockers,
        current_blockers=sorted(set(current_blockers)),
        current_freshness_warnings=sorted(set(current_freshness_warnings)),
        pit_blockers=pit_blockers,
        pit_contract_blockers=pit_contract_blockers,
        pit_runtime_blockers=pit_runtime_blockers,
        degraded_nonblocking=degraded,
    )
