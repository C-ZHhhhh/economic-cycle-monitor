"""Stateless source resolver for current and date-granular PIT observations."""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_quality.scope import PITAvailabilityStatus
from app.database.models import (
    DataQualityEvent,
    IndicatorRegistry,
    IndicatorSourceMapping,
    ObservationVintage,
    RawObservation,
)
from app.source_registry.config import load_source_registry, storage_source_name
from app.source_registry.health import INTEGRITY_EVENT_TYPES, indicator_source_health
from app.source_registry.schemas import (
    EffectiveObservation,
    FallbackReason,
    MappingQuality,
    RuntimeHealth,
    SourceRegistryCatalog,
    SourceRegistryPolicy,
    SourceSystem,
)


class SourceResolutionError(RuntimeError):
    """Base fail-closed resolution error."""


class PITGranularityUnsupported(SourceResolutionError):
    """Timestamp precision was requested from a date-granular contract."""


class PITCoverageUnverified(SourceResolutionError):
    """The indicator policy does not approve safe date-EOD ALFRED resolution."""

    def __init__(self, indicator_id: str, capability: str) -> None:
        super().__init__(f"PIT_UNSAFE:PIT_COVERAGE_UNVERIFIED:{indicator_id}:{capability}")
        self.indicator_id = indicator_id
        self.availability_status = PITAvailabilityStatus.UNSAFE


class PITSnapshotNotCached(SourceResolutionError):
    """The requested exact-date ALFRED information set is not stored locally."""


class SourceIntegrityError(SourceResolutionError):
    """An unresolved identity/value/normalization conflict prevents resolution."""


def _release_date(details: dict[str, object]) -> date | None:
    value = details.get("release_date")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _active_mapping(
    session: Session, policy: SourceRegistryPolicy
) -> IndicatorSourceMapping | None:
    if policy.primary_source is SourceSystem.FRED_CURRENT:
        return None
    return session.scalar(
        select(IndicatorSourceMapping)
        .where(
            IndicatorSourceMapping.indicator_id == policy.indicator_id,
            IndicatorSourceMapping.source == storage_source_name(policy.primary_source.value),
            IndicatorSourceMapping.source_series_id == policy.primary_source_identity,
            IndicatorSourceMapping.enabled.is_(True),
        )
        .order_by(IndicatorSourceMapping.id.desc())
        .limit(1)
    )


def _assert_no_integrity_conflict(session: Session, indicator_id: str) -> None:
    conflict = session.scalar(
        select(DataQualityEvent.event_type)
        .where(
            DataQualityEvent.indicator_id == indicator_id,
            DataQualityEvent.event_type.in_(INTEGRITY_EVENT_TYPES),
            DataQualityEvent.resolved_at.is_(None),
        )
        .limit(1)
    )
    if conflict is not None:
        raise SourceIntegrityError(f"SOURCE_INTEGRITY_ERROR:{conflict}")


def _mapping_quality(mapping: IndicatorSourceMapping | None) -> MappingQuality:
    if mapping is None or mapping.mapping_status == "valid":
        return MappingQuality.VALID
    if mapping.mapping_status == "valid_with_warning":
        return MappingQuality.VALID_WITH_METADATA_WARNING
    raise SourceIntegrityError(f"MAPPING_NOT_PRODUCTION_ELIGIBLE:{mapping.mapping_status}")


def _resolve_pit(
    session: Session,
    policy: SourceRegistryPolicy,
    indicator: IndicatorRegistry,
    *,
    start: date,
    end: date,
    as_of: date,
    registry_hash: str,
) -> list[EffectiveObservation]:
    if (
        policy.vintage.capability != "PIT_SAFE_VIA_ALFRED_PROXY"
        or policy.vintage.backtest_safety != "PIT_SAFE_DATE_EOD"
        or policy.vintage.information_set_semantics != "END_OF_DAY"
    ):
        raise PITCoverageUnverified(policy.indicator_id, policy.vintage.capability)
    snapshot_exists = session.scalar(
        select(ObservationVintage.id)
        .where(
            ObservationVintage.indicator_id == policy.indicator_id,
            ObservationVintage.source == "fred",
            ObservationVintage.vintage_date == as_of,
            ObservationVintage.release_stage == "as_known_on",
        )
        .limit(1)
    )
    if snapshot_exists is None:
        raise PITSnapshotNotCached(
            f"PIT_SNAPSHOT_NOT_CACHED:{policy.indicator_id}:{as_of.isoformat()}"
        )
    vintages = session.scalars(
        select(ObservationVintage)
        .where(
            ObservationVintage.indicator_id == policy.indicator_id,
            ObservationVintage.source == "fred",
            ObservationVintage.observation_date.between(start, end),
            ObservationVintage.observation_date <= as_of,
            ObservationVintage.vintage_date == as_of,
            ObservationVintage.release_stage == "as_known_on",
        )
        .order_by(
            ObservationVintage.observation_date,
            ObservationVintage.vintage_date.desc(),
            ObservationVintage.revision_number.desc(),
        )
    ).all()
    latest_by_date: dict[date, ObservationVintage] = {}
    for item in vintages:
        latest_by_date.setdefault(item.observation_date, item)
    return [
        EffectiveObservation(
            indicator_id=policy.indicator_id,
            observation_date=item.observation_date,
            value=Decimal(item.current_value),
            unit=policy.canonical.unit,
            frequency=policy.canonical.frequency,
            source_used="fred",
            source_identity=indicator.fred_series_id or policy.primary_source_identity,
            source_policy_version=policy.registry_policy_version,
            registry_hash=registry_hash,
            mapping_version=f"alfred:{policy.registry_policy_version}",
            is_fallback=False,
            resolution_reason="ALFRED_AS_OF_DATE",
            mapping_quality=MappingQuality.VALID,
            provider_health=RuntimeHealth.HEALTHY,
            indicator_source_health=RuntimeHealth.HEALTHY,
            release_date=item.release_date,
            retrieved_at=item.retrieved_at,
            vintage_as_of=as_of,
            vintage_source=SourceSystem.ALFRED_VINTAGE.value,
            is_point_in_time_safe=True,
        )
        for item in latest_by_date.values()
    ]


def _fallback_reason(policy: SourceRegistryPolicy, health: RuntimeHealth) -> FallbackReason | None:
    if health is RuntimeHealth.STALE:
        candidate = FallbackReason.PRIMARY_STALE_BEYOND_GRACE
    elif health is RuntimeHealth.UNAVAILABLE:
        candidate = FallbackReason.PRIMARY_TEMPORARILY_UNAVAILABLE
    else:
        return None
    return candidate if candidate in policy.fallback.allowed_reasons else None


def _current_rows(
    session: Session,
    indicator_id: str,
    source: str,
    start: date,
    end: date,
) -> Sequence[RawObservation]:
    return session.scalars(
        select(RawObservation)
        .where(
            RawObservation.indicator_id == indicator_id,
            RawObservation.source == source,
            RawObservation.observation_date.between(start, end),
        )
        .order_by(RawObservation.observation_date)
    ).all()


def get_effective_observations(
    session: Session,
    indicator_id: str,
    start: date,
    end: date,
    as_of: date | datetime | None = None,
    *,
    registry: SourceRegistryCatalog | None = None,
    today: date | None = None,
    evaluated_at: datetime | None = None,
) -> list[EffectiveObservation]:
    """Resolve canonical observations without comparing-and-choosing values."""
    if isinstance(as_of, datetime):
        raise PITGranularityUnsupported("PIT_GRANULARITY_UNSUPPORTED")
    if start > end:
        raise ValueError("start must not be after end")
    source_registry = registry or load_source_registry()
    try:
        policy = source_registry.by_indicator()[indicator_id]
    except KeyError as error:
        raise SourceResolutionError(f"UNKNOWN_INDICATOR:{indicator_id}") from error
    indicator = session.scalar(
        select(IndicatorRegistry).where(IndicatorRegistry.indicator_id == indicator_id)
    )
    if indicator is None:
        raise SourceResolutionError(f"INDICATOR_NOT_MATERIALIZED:{indicator_id}")
    if as_of is not None:
        return _resolve_pit(
            session,
            policy,
            indicator,
            start=start,
            end=end,
            as_of=as_of,
            registry_hash=source_registry.registry_hash,
        )

    _assert_no_integrity_conflict(session, indicator_id)
    mapping = _active_mapping(session, policy)
    quality = _mapping_quality(mapping)
    health = indicator_source_health(session, policy, today=today, evaluated_at=evaluated_at)
    if health.runtime_health is RuntimeHealth.ERROR:
        raise SourceIntegrityError("PRIMARY_SOURCE_HEALTH_ERROR")
    primary_source = storage_source_name(policy.primary_source.value)
    primary_rows = {
        item.observation_date: item
        for item in _current_rows(session, indicator_id, primary_source, start, end)
    }
    fallback_source = (
        storage_source_name(policy.fallback_source.value)
        if policy.fallback_source is not None
        else None
    )
    fallback_rows = (
        {
            item.observation_date: item
            for item in _current_rows(session, indicator_id, fallback_source, start, end)
        }
        if fallback_source is not None
        else {}
    )
    runtime_fallback = _fallback_reason(policy, health.runtime_health)
    results: list[EffectiveObservation] = []
    for observation_date in sorted(set(primary_rows) | set(fallback_rows)):
        primary = primary_rows.get(observation_date)
        reason: str
        is_fallback = False
        selected = primary
        source_identity = policy.primary_source_identity
        source_used = primary_source
        if (
            observation_date < policy.history.earliest_primary_date
            and policy.history.extension_policy == "FALLBACK_BEFORE_PRIMARY_START"
            and FallbackReason.PRIMARY_BEFORE_OFFICIAL_HISTORY in policy.fallback.allowed_reasons
        ):
            selected = fallback_rows.get(observation_date)
            is_fallback = selected is not None
            reason = FallbackReason.PRIMARY_BEFORE_OFFICIAL_HISTORY.value
        elif runtime_fallback is not None:
            selected = fallback_rows.get(observation_date)
            is_fallback = selected is not None
            reason = runtime_fallback.value
        else:
            reason = "PRIMARY_POLICY"
        if selected is None:
            continue
        if is_fallback:
            source_used = fallback_source or "fred"
            source_identity = indicator.fred_series_id or ""
        results.append(
            EffectiveObservation(
                indicator_id=indicator_id,
                observation_date=selected.observation_date,
                value=Decimal(selected.value),
                unit=policy.canonical.unit,
                frequency=policy.canonical.frequency,
                source_used=source_used,
                source_identity=source_identity,
                source_policy_version=policy.registry_policy_version,
                registry_hash=source_registry.registry_hash,
                mapping_version=(
                    mapping.mapping_version
                    if mapping is not None and not is_fallback
                    else f"fred-reference:{indicator.config_version}"
                ),
                is_fallback=is_fallback,
                resolution_reason=reason,
                mapping_quality=quality,
                provider_health=health.runtime_health,
                indicator_source_health=health.runtime_health,
                release_date=_release_date(selected.source_details_json),
                retrieved_at=selected.retrieved_at,
                vintage_as_of=None,
                vintage_source=None,
                is_point_in_time_safe=False,
            )
        )
    return results
