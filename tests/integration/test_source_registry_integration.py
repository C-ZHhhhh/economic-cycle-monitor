"""PostgreSQL integration coverage for Stage 2B-5 source resolution."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.data_pipeline.schemas import PipelineSelection
from app.data_quality.service import run_database_quality_checks
from app.database.models import (
    DataQualityEvent,
    DataSourceRequest,
    IndicatorRegistry,
    IndicatorSourceMapping,
    ObservationVintage,
    RawObservation,
)
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.source_mappings.config import SOURCE_MAPPING_PATHS, load_source_mapping_catalog_for
from app.source_mappings.repository import seed_source_mappings
from app.source_registry.audit import audit_materialized_policy
from app.source_registry.config import load_source_registry, storage_source_name
from app.source_registry.health import (
    expected_latest_period,
    historical_audit_event_is_current,
    indicator_source_health,
)
from app.source_registry.readiness import validate_data_layer
from app.source_registry.resolver import (
    PITCoverageUnverified,
    SourceIntegrityError,
    get_effective_observations,
)
from app.source_registry.schemas import RuntimeHealth, SourceRegistryCatalog

pytestmark = pytest.mark.integration


def _seed_all(session: Session) -> SourceRegistryCatalog:
    registry = load_source_registry()
    seed_indicator_catalog(session, load_indicator_catalog(), registry)
    for source in SOURCE_MAPPING_PATHS:
        seed_source_mappings(session, load_source_mapping_catalog_for(source), registry)
    return registry


def _request(session: Session, source: str, when: datetime, *, success: bool = True) -> None:
    session.add(
        DataSourceRequest(
            source=source,
            endpoint="offline-fixture",
            series_id=None,
            request_parameters_json={},
            started_at=when,
            finished_at=when,
            success=success,
            attempt_count=1,
            from_cache=False,
        )
    )


def _observation(
    session: Session,
    indicator_id: str,
    source: str,
    observation_date: date,
    value: str = "1",
) -> None:
    session.add(
        RawObservation(
            indicator_id=indicator_id,
            observation_date=observation_date,
            value=Decimal(value),
            source=source,
            retrieved_at=datetime(2024, 6, 15, tzinfo=UTC),
            raw_payload_hash=(indicator_id + source).encode().hex()[:64].ljust(64, "0"),
            source_details_json={"canonical_value": value},
        )
    )


def test_registry_seed_materializes_all_primary_policies_without_drift(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    rows = list(postgres_session.scalars(select(IndicatorRegistry)).all())
    assert len(rows) == 33
    assert audit_materialized_policy(registry, rows) == []


def test_dol_empty_static_warning_is_normalized_to_valid(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    mapping = postgres_session.scalar(
        select(IndicatorSourceMapping).where(
            IndicatorSourceMapping.indicator_id == "US_INITIAL_CLAIMS",
            IndicatorSourceMapping.source == "dol",
            IndicatorSourceMapping.enabled.is_(True),
        )
    )
    assert mapping is not None
    mapping.mapping_status = "valid_with_warning"
    mapping.mapping_warnings_json = []
    mapping.metadata_checked_at = datetime.now(UTC)
    seed_source_mappings(postgres_session, load_source_mapping_catalog_for("dol"), registry)
    assert mapping.mapping_status == "valid"


def test_historical_provider_failure_is_superseded_by_later_success(
    postgres_session: Session,
) -> None:
    _seed_all(postgres_session)
    event = DataQualityEvent(
        indicator_id="US_UNEMPLOYMENT_RATE",
        event_type="source_business_error",
        severity="warning",
        message="sanitized historical failure",
        details_json={"source": "bls"},
        event_fingerprint="a" * 64,
        detected_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    postgres_session.add(event)
    _request(postgres_session, "bls", datetime(2024, 2, 1, tzinfo=UTC))
    postgres_session.flush()
    assert not historical_audit_event_is_current(postgres_session, event)
    assert event.resolved_at is None


def test_dol_lag_is_runtime_degraded_not_mapping_warning(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    policy = registry.by_indicator()["US_INITIAL_CLAIMS"]
    _observation(postgres_session, policy.indicator_id, "dol", date(2026, 7, 25))
    _request(postgres_session, "dol", datetime(2026, 8, 13, tzinfo=UTC))
    postgres_session.flush()
    snapshot = indicator_source_health(postgres_session, policy, today=date(2026, 8, 13))
    assert snapshot.mapping_quality.value == "VALID"
    assert snapshot.runtime_health is RuntimeHealth.DEGRADED
    assert snapshot.reasons == ["EXPECTED_PERIOD_LAG_WITHIN_GRACE"]


def test_monthly_grace_is_measured_after_expected_release_date(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    policy = registry.by_indicator()["US_CPI"]
    _observation(postgres_session, policy.indicator_id, "bls", date(2098, 6, 1))
    _request(postgres_session, "bls", datetime(2098, 8, 13, tzinfo=UTC))
    postgres_session.flush()
    snapshot = indicator_source_health(postgres_session, policy, today=date(2098, 8, 13))
    assert snapshot.expected_latest_period == date(2098, 7, 1)
    assert snapshot.runtime_health is RuntimeHealth.DEGRADED
    assert snapshot.reasons == ["EXPECTED_PERIOD_LAG_WITHIN_GRACE"]


def _reset_indicator_observations(session: Session, indicator_id: str) -> None:
    session.execute(delete(RawObservation).where(RawObservation.indicator_id == indicator_id))


@pytest.mark.parametrize("indicator_id", ["US_BUSINESS_INVENTORIES", "US_INVENTORY_SALES_RATIO"])
def test_mtis_may_is_current_before_june_release_and_indicators_are_independent(
    postgres_session: Session, indicator_id: str
) -> None:
    registry = _seed_all(postgres_session)
    policy = registry.by_indicator()[indicator_id]
    _reset_indicator_observations(postgres_session, indicator_id)
    _observation(postgres_session, indicator_id, "census", date(2026, 5, 1))
    _observation(postgres_session, indicator_id, "fred", date(2026, 5, 1))
    _request(postgres_session, "census", datetime(2026, 8, 13, tzinfo=UTC))
    postgres_session.flush()

    evaluated_at = datetime.fromisoformat("2026-08-13T23:59:59-04:00")
    snapshot = indicator_source_health(postgres_session, policy, evaluated_at=evaluated_at)
    rows = get_effective_observations(
        postgres_session,
        indicator_id,
        date(2026, 5, 1),
        date(2026, 5, 1),
        registry=registry,
        evaluated_at=evaluated_at,
    )
    assert snapshot.expected_latest_period == date(2026, 5, 1)
    assert snapshot.runtime_health is RuntimeHealth.HEALTHY
    assert rows[-1].source_used == "census"
    assert not rows[-1].is_fallback
    assert rows[-1].resolution_reason == "PRIMARY_POLICY"
    before_release = indicator_source_health(
        postgres_session,
        policy,
        evaluated_at=datetime.fromisoformat("2026-08-14T09:59:59-04:00"),
    )
    assert before_release.expected_latest_period == date(2026, 5, 1)
    assert before_release.runtime_health is RuntimeHealth.HEALTHY


def test_mtis_missing_period_degrades_then_stales_and_recovers(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    indicator_id = "US_BUSINESS_INVENTORIES"
    policy = registry.by_indicator()[indicator_id]
    _reset_indicator_observations(postgres_session, indicator_id)
    _observation(postgres_session, indicator_id, "census", date(2026, 5, 1))
    _observation(postgres_session, indicator_id, "fred", date(2026, 6, 1))
    _request(postgres_session, "census", datetime(2026, 8, 14, 14, 0, tzinfo=UTC))
    postgres_session.flush()

    within_grace = datetime.fromisoformat("2026-08-14T10:00:01-04:00")
    degraded = indicator_source_health(postgres_session, policy, evaluated_at=within_grace)
    assert degraded.expected_latest_period == date(2026, 6, 1)
    assert degraded.runtime_health is RuntimeHealth.DEGRADED

    after_grace = datetime.fromisoformat("2026-08-30T10:00:00-04:00")
    stale = indicator_source_health(postgres_session, policy, evaluated_at=after_grace)
    fallback = get_effective_observations(
        postgres_session,
        indicator_id,
        date(2026, 6, 1),
        date(2026, 6, 1),
        registry=registry,
        evaluated_at=after_grace,
    )
    assert stale.runtime_health is RuntimeHealth.STALE
    assert fallback[-1].source_used == "fred"
    assert fallback[-1].is_fallback

    _observation(postgres_session, indicator_id, "census", date(2026, 6, 1))
    _request(postgres_session, "census", datetime(2026, 8, 30, 13, 59, tzinfo=UTC))
    postgres_session.flush()
    recovered = indicator_source_health(postgres_session, policy, evaluated_at=after_grace)
    primary = get_effective_observations(
        postgres_session,
        indicator_id,
        date(2026, 6, 1),
        date(2026, 6, 1),
        registry=registry,
        evaluated_at=after_grace,
    )
    assert recovered.runtime_health is RuntimeHealth.HEALTHY
    assert primary[-1].source_used == "census"
    assert not primary[-1].is_fallback


def test_mtis_unknown_future_calendar_degrades_without_hard_stale(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    indicator_id = "US_INVENTORY_SALES_RATIO"
    policy = registry.by_indicator()[indicator_id]
    _reset_indicator_observations(postgres_session, indicator_id)
    _observation(postgres_session, indicator_id, "census", date(2026, 7, 1))
    _request(postgres_session, "census", datetime(2026, 10, 1, tzinfo=UTC))
    postgres_session.flush()
    snapshot = indicator_source_health(
        postgres_session,
        policy,
        evaluated_at=datetime.fromisoformat("2026-10-01T12:00:00-04:00"),
    )
    assert snapshot.runtime_health is RuntimeHealth.DEGRADED
    assert snapshot.reasons == ["RELEASE_SCHEDULE_UNAVAILABLE"]
    assert not snapshot.schedule_is_certain


@pytest.mark.parametrize(
    ("indicator_id", "source"),
    [
        ("US_REAL_PCE", "bea"),
        ("US_REAL_DISPOSABLE_INCOME", "bea"),
        ("US_PCE_PRICE", "bea"),
        ("US_CORE_PCE_PRICE", "bea"),
        ("US_JOB_OPENINGS", "bls"),
        ("US_HIRES", "bls"),
        ("US_QUITS", "bls"),
        ("US_CONSUMER_CREDIT", "federal_reserve"),
        ("US_REVOLVING_CREDIT", "federal_reserve"),
    ],
)
def test_v3_fixed_calendars_keep_nine_june_primaries_current_on_august_25(
    postgres_session: Session, indicator_id: str, source: str
) -> None:
    registry = _seed_all(postgres_session)
    policy = registry.by_indicator()[indicator_id]
    _reset_indicator_observations(postgres_session, indicator_id)
    _observation(postgres_session, indicator_id, source, date(2026, 6, 1))
    _observation(postgres_session, indicator_id, "fred", date(2026, 6, 1))
    _request(postgres_session, source, datetime(2026, 8, 25, 16, 0, tzinfo=UTC))
    postgres_session.flush()
    evaluated_at = datetime.fromisoformat("2026-08-25T23:59:59-04:00")

    snapshot = indicator_source_health(postgres_session, policy, evaluated_at=evaluated_at)
    rows = get_effective_observations(
        postgres_session,
        indicator_id,
        date(2026, 6, 1),
        date(2026, 6, 1),
        registry=registry,
        evaluated_at=evaluated_at,
    )
    assert snapshot.expected_latest_period == date(2026, 6, 1)
    assert snapshot.runtime_health is RuntimeHealth.HEALTHY
    assert rows[-1].source_used == source
    assert not rows[-1].is_fallback


def test_bea_pio_missing_released_period_degrades_then_allows_fallback(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    indicator_id = "US_REAL_PCE"
    policy = registry.by_indicator()[indicator_id]
    _reset_indicator_observations(postgres_session, indicator_id)
    _observation(postgres_session, indicator_id, "bea", date(2026, 6, 1))
    _observation(postgres_session, indicator_id, "fred", date(2026, 7, 1))
    _request(postgres_session, "bea", datetime(2026, 8, 26, 12, 30, tzinfo=UTC))
    postgres_session.flush()

    within_grace = datetime.fromisoformat("2026-08-26T08:30:01-04:00")
    degraded = indicator_source_health(postgres_session, policy, evaluated_at=within_grace)
    assert degraded.expected_latest_period == date(2026, 7, 1)
    assert degraded.runtime_health is RuntimeHealth.DEGRADED

    after_grace = datetime.fromisoformat("2026-09-10T08:30:00-04:00")
    stale = indicator_source_health(postgres_session, policy, evaluated_at=after_grace)
    rows = get_effective_observations(
        postgres_session,
        indicator_id,
        date(2026, 7, 1),
        date(2026, 7, 1),
        registry=registry,
        evaluated_at=after_grace,
    )
    assert stale.runtime_health is RuntimeHealth.STALE
    assert rows[-1].source_used == "fred"
    assert rows[-1].is_fallback


def test_current_resolver_uses_healthy_primary(postgres_session: Session) -> None:
    registry = _seed_all(postgres_session)
    _observation(postgres_session, "US_CPI", "bls", date(2024, 1, 1), "300")
    _observation(postgres_session, "US_CPI", "fred", date(2024, 1, 1), "300")
    _request(postgres_session, "bls", datetime(2024, 2, 10, tzinfo=UTC))
    postgres_session.flush()
    rows = get_effective_observations(
        postgres_session,
        "US_CPI",
        date(2024, 1, 1),
        date(2024, 1, 1),
        registry=registry,
        today=date(2024, 2, 10),
    )
    assert len(rows) == 1
    assert rows[0].source_used == "bls"
    assert not rows[0].is_fallback
    assert rows[0].resolution_reason == "PRIMARY_POLICY"


def test_stale_primary_activates_only_approved_fred_fallback(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    _observation(postgres_session, "US_CPI", "bls", date(2099, 1, 1), "300")
    _observation(postgres_session, "US_CPI", "fred", date(2099, 3, 1), "301")
    _request(postgres_session, "bls", datetime(2099, 4, 20, tzinfo=UTC))
    postgres_session.flush()
    snapshot = indicator_source_health(
        postgres_session,
        registry.by_indicator()["US_CPI"],
        today=date(2099, 4, 20),
    )
    assert snapshot.runtime_health is RuntimeHealth.STALE
    rows = get_effective_observations(
        postgres_session,
        "US_CPI",
        date(2099, 3, 1),
        date(2099, 3, 1),
        registry=registry,
        today=date(2099, 4, 20),
    )
    assert len(rows) == 1
    assert rows[0].source_used == "fred"
    assert rows[0].is_fallback
    assert rows[0].resolution_reason == "PRIMARY_STALE_BEYOND_GRACE"


def test_value_conflict_fails_closed_instead_of_falling_back(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    _observation(postgres_session, "US_CPI", "bls", date(2024, 1, 1), "300")
    _observation(postgres_session, "US_CPI", "fred", date(2024, 1, 1), "301")
    postgres_session.add(
        DataQualityEvent(
            indicator_id="US_CPI",
            event_type="source_value_conflict",
            severity="error",
            message="offline fixture conflict",
            details_json={},
            event_fingerprint="b" * 64,
            detected_at=datetime(2024, 2, 1, tzinfo=UTC),
        )
    )
    postgres_session.flush()
    with pytest.raises(SourceIntegrityError, match="source_value_conflict"):
        get_effective_observations(
            postgres_session,
            "US_CPI",
            date(2024, 1, 1),
            date(2024, 1, 1),
            registry=registry,
            today=date(2024, 2, 10),
        )


def test_unverified_alfred_policy_rejects_pit_request(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    with pytest.raises(PITCoverageUnverified, match="PIT_COVERAGE_UNVERIFIED"):
        get_effective_observations(
            postgres_session,
            "US_CPI",
            date(2020, 1, 1),
            date(2020, 1, 1),
            date(2020, 6, 30),
            registry=registry,
        )


def test_validated_gdp_date_uses_only_alfred_vintages_visible_as_of_date(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    postgres_session.add(
        ObservationVintage(
            indicator_id="US_REAL_GDP",
            observation_date=date(2020, 1, 1),
            vintage_date=date(2020, 6, 30),
            release_stage="as_known_on",
            revision_number=0,
            current_value=Decimal("19000"),
            source="fred",
            source_vintage_date=date(2020, 6, 30),
            retrieved_at=datetime(2024, 6, 15, tzinfo=UTC),
            raw_payload_hash="c" * 64,
            source_details_json={},
        )
    )
    postgres_session.flush()
    rows = get_effective_observations(
        postgres_session,
        "US_REAL_GDP",
        date(2020, 1, 1),
        date(2020, 1, 1),
        date(2020, 6, 30),
        registry=registry,
    )
    assert len(rows) == 1
    assert rows[0].vintage_as_of == date(2020, 6, 30)
    assert rows[0].vintage_source == "ALFRED_VINTAGE"
    assert rows[0].is_point_in_time_safe


def test_current_and_pit_readiness_pass_after_all_live_gates(
    postgres_session: Session,
) -> None:
    registry = _seed_all(postgres_session)
    today = date(2024, 6, 15)
    for source in {storage_source_name(item.primary_source.value) for item in registry.policies}:
        _request(postgres_session, source, datetime(2024, 6, 15, tzinfo=UTC))
    for policy in registry.policies:
        _observation(
            postgres_session,
            policy.indicator_id,
            storage_source_name(policy.primary_source.value),
            expected_latest_period(policy, today=today),
        )
    postgres_session.flush()
    readiness = validate_data_layer(postgres_session, registry=registry, today=today)
    assert readiness.current_data_ready
    assert readiness.current_blockers == []
    assert readiness.pit_backtest_ready
    assert readiness.pit_blockers == []


def test_quality_check_does_not_mix_source_separated_vintage_grain(
    postgres_session: Session,
) -> None:
    _seed_all(postgres_session)
    for source in ("fred", "bls"):
        postgres_session.add(
            ObservationVintage(
                indicator_id="US_UNEMPLOYMENT_RATE",
                observation_date=date(2025, 1, 1),
                vintage_date=date(2025, 2, 1),
                release_stage="revision",
                revision_number=0,
                current_value=Decimal("4.1"),
                source=source,
                source_vintage_date=date(2025, 2, 1),
                retrieved_at=datetime(2025, 2, 1, tzinfo=UTC),
                raw_payload_hash=source.encode().hex().ljust(64, "0"),
                source_details_json={},
            )
        )
    postgres_session.flush()

    summary = run_database_quality_checks(
        postgres_session,
        PipelineSelection(indicator_id="US_UNEMPLOYMENT_RATE"),
    )

    duplicate = postgres_session.scalar(
        select(DataQualityEvent.id).where(
            DataQualityEvent.run_id == summary.run_id,
            DataQualityEvent.event_type == "duplicate_vintage",
        )
    )
    assert duplicate is None
