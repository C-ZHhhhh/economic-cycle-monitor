"""Offline contract coverage for the DLCR-005 current freshness authority."""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import ValidationError

from app.source_registry.config import load_source_registry
from app.source_registry.current_freshness import (
    FULL_M3_FAMILY,
    FULL_M3_INDICATORS,
    CurrentFreshnessCatalog,
    CurrentReleaseEntry,
    CurrentReleaseFamily,
    ReleaseStatus,
    evaluate_current_freshness,
    load_current_freshness_catalog,
)


@pytest.fixture(scope="module")
def catalog() -> CurrentFreshnessCatalog:
    """Load the tracked independent current freshness authority."""
    return load_current_freshness_catalog()


def _entry(
    period: str,
    release: str | None,
    *,
    status: ReleaseStatus = ReleaseStatus.SCHEDULED,
    previous: str | None = None,
) -> CurrentReleaseEntry:
    return CurrentReleaseEntry(
        observation_period=date.fromisoformat(period),
        scheduled_release_timestamp=(
            datetime.fromisoformat(release) if release is not None else None
        ),
        status=status,
        official_source_url="https://www.census.gov/manufacturing/m3/release_schedule.html",
        verified_at=datetime.fromisoformat("2026-09-07T00:00:00-04:00"),
        previous_scheduled_timestamp=(
            datetime.fromisoformat(previous) if previous is not None else None
        ),
        revision_reason="official reschedule" if previous is not None else None,
        revision_evidence=(
            "https://www.census.gov/economic-indicators/" if previous is not None else None
        ),
    )


def test_authority_binds_frozen_registry_without_changing_it(
    catalog: CurrentFreshnessCatalog,
) -> None:
    registry = load_source_registry()
    assert catalog.current_freshness_version == "current-freshness-v1"
    assert catalog.required_source_registry_version == "stage2b5-v3"
    assert catalog.required_source_registry_hash == registry.registry_hash
    assert registry.registry_hash == (
        "7a13095907733a1d6092ef5231c67de7528886c7eda9eeb73ed2166764834758"
    )


def test_only_two_approved_full_m3_indicators_bind(
    catalog: CurrentFreshnessCatalog,
) -> None:
    family = catalog.release_families[0]
    assert family.release_family == FULL_M3_FAMILY
    assert set(family.indicator_ids) == FULL_M3_INDICATORS
    assert catalog.family_for_indicator("US_HOUSING_STARTS") is None
    assert catalog.family_for_indicator("US_BUILDING_PERMITS") is None
    assert catalog.family_for_indicator("US_BUSINESS_INVENTORIES") is None
    assert catalog.family_for_indicator("US_INVENTORY_SALES_RATIO") is None


def test_full_m3_calendar_preserves_official_periods_statuses_and_timezone(
    catalog: CurrentFreshnessCatalog,
) -> None:
    family = catalog.release_families[0]
    assert family.timezone == "America/New_York"
    assert family.operational_grace == "PT48H"
    assert [entry.observation_period for entry in family.calendar] == [
        date(2026, month, 1) for month in range(5, 12)
    ]
    assert [entry.status for entry in family.calendar] == [
        ReleaseStatus.RELEASED,
        ReleaseStatus.RELEASED,
        ReleaseStatus.RELEASED,
        ReleaseStatus.SCHEDULED,
        ReleaseStatus.SCHEDULED,
        ReleaseStatus.SCHEDULED,
        ReleaseStatus.TBD,
    ]


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-08-31T23:59:59-04:00", date(2026, 6, 1)),
        ("2026-09-01T23:59:59-04:00", date(2026, 6, 1)),
        ("2026-09-02T09:59:59-04:00", date(2026, 6, 1)),
        ("2026-09-02T10:00:00-04:00", date(2026, 7, 1)),
        ("2026-09-02T14:00:00+00:00", date(2026, 7, 1)),
    ],
)
def test_july_2026_regression_has_no_old_false_stale_window(
    catalog: CurrentFreshnessCatalog,
    instant: str,
    expected: date,
) -> None:
    result = evaluate_current_freshness(
        catalog,
        "US_DURABLE_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat(instant),
    )
    assert result is not None
    assert result.period == expected
    assert not result.coverage_expired


def test_advance_m3_time_does_not_activate_full_report(
    catalog: CurrentFreshnessCatalog,
) -> None:
    advance = evaluate_current_freshness(
        catalog,
        "US_CORE_CAPITAL_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat("2026-09-02T08:30:00-04:00"),
    )
    assert advance is not None
    assert advance.period == date(2026, 6, 1)


def test_current_only_authority_is_not_applied_retroactively(
    catalog: CurrentFreshnessCatalog,
) -> None:
    assert (
        evaluate_current_freshness(
            catalog,
            "US_DURABLE_GOODS_ORDERS",
            evaluated_at=datetime.fromisoformat("2024-06-15T12:00:00-04:00"),
        )
        is None
    )


def test_full_report_uses_absolute_48_hour_grace(catalog: CurrentFreshnessCatalog) -> None:
    result = evaluate_current_freshness(
        catalog,
        "US_DURABLE_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat("2026-09-02T10:00:00-04:00"),
    )
    assert result is not None
    assert result.grace_ends_at == datetime.fromisoformat("2026-09-04T10:00:00-04:00")


def test_est_and_edt_offsets_are_preserved(catalog: CurrentFreshnessCatalog) -> None:
    family = catalog.release_families[0]
    july = family.calendar[2]
    september = family.calendar[4]
    assert july.effective_release_timestamp is not None
    assert september.effective_release_timestamp is not None
    july_offset = july.effective_release_timestamp.utcoffset()
    september_offset = september.effective_release_timestamp.utcoffset()
    assert july_offset is not None and september_offset is not None
    assert july_offset.total_seconds() == -4 * 3600
    assert september_offset.total_seconds() == -5 * 3600


def test_absolute_grace_is_not_corrupted_by_dst_transition(
    catalog: CurrentFreshnessCatalog,
) -> None:
    family = catalog.release_families[0]
    release = _entry("2026-10-01", "2026-10-31T10:00:00-04:00")
    next_period = _entry("2026-11-01", None, status=ReleaseStatus.TBD)
    modified_family = family.model_copy(update={"calendar": [release, next_period]})
    modified = catalog.model_copy(update={"release_families": [modified_family]})
    result = evaluate_current_freshness(
        modified,
        "US_DURABLE_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat("2026-10-31T10:00:00-04:00"),
    )
    assert result is not None and result.effective_release_timestamp is not None
    assert result.grace_ends_at is not None
    assert (result.grace_ends_at - result.effective_release_timestamp).total_seconds() == 48 * 3600
    assert result.grace_ends_at.astimezone(ZoneInfo("America/New_York")).isoformat() == (
        "2026-11-02T09:00:00-05:00"
    )


def test_tbd_and_suspended_never_create_synthetic_dates() -> None:
    for status in (ReleaseStatus.TBD, ReleaseStatus.SUSPENDED):
        entry = _entry("2027-01-01", None, status=status)
        assert entry.effective_release_timestamp is None


def test_tbd_is_explicit_diagnostic_not_data_stale(
    catalog: CurrentFreshnessCatalog,
) -> None:
    result = evaluate_current_freshness(
        catalog,
        "US_DURABLE_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat("2026-12-03T10:00:00-05:00"),
    )
    assert result is not None
    assert result.period == date(2026, 10, 1)
    assert result.diagnostics == ["CURRENT_RELEASE_DATE_TBD"]
    assert "DATA_STALE" not in result.diagnostics
    assert not result.coverage_expired


def test_rescheduled_entry_uses_new_time_and_preserves_prior_time() -> None:
    entry = _entry(
        "2026-07-01",
        "2026-09-03T10:00:00-04:00",
        status=ReleaseStatus.RESCHEDULED,
        previous="2026-09-02T10:00:00-04:00",
    )
    assert entry.effective_release_timestamp == datetime.fromisoformat("2026-09-03T10:00:00-04:00")
    assert entry.previous_scheduled_timestamp == datetime.fromisoformat("2026-09-02T10:00:00-04:00")


def test_rescheduled_period_does_not_activate_at_old_timestamp(
    catalog: CurrentFreshnessCatalog,
) -> None:
    family = catalog.release_families[0]
    rescheduled = _entry(
        "2026-07-01",
        "2026-09-03T10:00:00-04:00",
        status=ReleaseStatus.RESCHEDULED,
        previous="2026-09-02T10:00:00-04:00",
    )
    modified_family = family.model_copy(
        update={"calendar": [*family.calendar[:2], rescheduled, *family.calendar[3:]]}
    )
    modified = catalog.model_copy(update={"release_families": [modified_family]})
    at_old = evaluate_current_freshness(
        modified,
        "US_DURABLE_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat("2026-09-02T10:00:00-04:00"),
    )
    at_new = evaluate_current_freshness(
        modified,
        "US_DURABLE_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat("2026-09-03T10:00:00-04:00"),
    )
    assert at_old is not None and at_new is not None
    assert at_old.period == date(2026, 6, 1)
    assert at_new.period == date(2026, 7, 1)


def test_suspended_next_period_is_calendar_diagnostic(
    catalog: CurrentFreshnessCatalog,
) -> None:
    family = catalog.release_families[0]
    suspended = _entry("2026-11-01", None, status=ReleaseStatus.SUSPENDED)
    modified_family = family.model_copy(update={"calendar": [*family.calendar[:-1], suspended]})
    modified = catalog.model_copy(update={"release_families": [modified_family]})
    result = evaluate_current_freshness(
        modified,
        "US_DURABLE_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat("2026-12-03T10:00:00-05:00"),
    )
    assert result is not None
    assert result.period == date(2026, 10, 1)
    assert result.diagnostics == ["CURRENT_RELEASE_SUSPENDED"]
    assert not result.coverage_expired


def test_released_actual_timestamp_not_manual_verification_activates_period() -> None:
    entry = CurrentReleaseEntry(
        observation_period=date(2026, 7, 1),
        scheduled_release_timestamp=datetime.fromisoformat("2026-09-02T10:00:00-04:00"),
        actual_release_timestamp=datetime.fromisoformat("2026-09-02T10:01:00-04:00"),
        status=ReleaseStatus.RELEASED,
        official_source_url="https://www.census.gov/manufacturing/m3/current/index.html",
        verified_at=datetime.fromisoformat("2026-09-07T12:00:00-04:00"),
    )
    assert entry.effective_release_timestamp == datetime.fromisoformat("2026-09-02T10:01:00-04:00")


def test_release_entry_contract_rejects_invalid_status_payloads() -> None:
    with pytest.raises(ValidationError, match="requires scheduled_release_timestamp"):
        _entry("2027-01-01", None)
    with pytest.raises(ValidationError, match="cannot carry a synthetic"):
        _entry("2027-01-01", "2027-03-01T10:00:00-05:00", status=ReleaseStatus.TBD)
    with pytest.raises(ValidationError, match="prior timestamp"):
        _entry(
            "2027-01-01",
            "2027-03-01T10:00:00-05:00",
            status=ReleaseStatus.RESCHEDULED,
        )


def test_family_rejects_duplicates_gaps_and_unapproved_membership(
    catalog: CurrentFreshnessCatalog,
) -> None:
    family = catalog.release_families[0]
    payload = family.model_dump()
    payload["calendar"] = [payload["calendar"][0], payload["calendar"][0]]
    with pytest.raises(ValidationError, match="must be unique"):
        CurrentReleaseFamily.model_validate(payload)
    payload = family.model_dump()
    payload["calendar"] = [payload["calendar"][0], payload["calendar"][2]]
    with pytest.raises(ValidationError, match="contiguous monthly"):
        CurrentReleaseFamily.model_validate(payload)
    payload = family.model_dump()
    payload["indicator_ids"] = ["US_DURABLE_GOODS_ORDERS", "US_HOUSING_STARTS"]
    with pytest.raises(ValidationError, match="two approved indicators"):
        CurrentReleaseFamily.model_validate(payload)


def test_unordered_periods_are_rejected(catalog: CurrentFreshnessCatalog) -> None:
    payload = catalog.release_families[0].model_dump()
    payload["calendar"] = list(reversed(payload["calendar"]))
    with pytest.raises(ValidationError, match="must be ordered"):
        CurrentReleaseFamily.model_validate(payload)


def test_wrong_new_york_offset_is_rejected(catalog: CurrentFreshnessCatalog) -> None:
    payload = catalog.release_families[0].model_dump()
    payload["calendar"][2]["scheduled_release_timestamp"] = datetime.fromisoformat(
        "2026-09-02T10:00:00-05:00"
    )
    payload["calendar"][2]["actual_release_timestamp"] = datetime.fromisoformat(
        "2026-09-02T10:00:00-05:00"
    )
    with pytest.raises(ValidationError, match="America/New_York offset"):
        CurrentReleaseFamily.model_validate(payload)


def test_missing_next_period_fails_closed_with_typed_diagnostic(
    catalog: CurrentFreshnessCatalog,
) -> None:
    family = catalog.release_families[0].model_copy(
        update={"calendar": catalog.release_families[0].calendar[:3]}
    )
    modified = catalog.model_copy(update={"release_families": [family]})
    result = evaluate_current_freshness(
        modified,
        "US_DURABLE_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat("2026-09-02T10:00:00-04:00"),
    )
    assert result is not None
    assert result.coverage_expired
    assert result.diagnostics == ["CURRENT_CALENDAR_COVERAGE_EXPIRED"]


def test_low_horizon_is_warning_and_three_future_periods_may_include_tbd(
    catalog: CurrentFreshnessCatalog,
) -> None:
    september = evaluate_current_freshness(
        catalog,
        "US_DURABLE_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat("2026-09-07T12:00:00-04:00"),
    )
    december = evaluate_current_freshness(
        catalog,
        "US_DURABLE_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat("2026-12-03T10:00:00-05:00"),
    )
    assert september is not None and december is not None
    assert september.maintenance_warnings == []
    assert len(catalog.release_families[0].calendar[3:]) == 4
    assert catalog.release_families[0].calendar[-1].status is ReleaseStatus.TBD
    assert december.maintenance_warnings == ["CURRENT_FRESHNESS_CALENDAR_LOW_HORIZON"]
    assert not december.coverage_expired

    tbd_october = _entry("2026-10-01", None, status=ReleaseStatus.TBD)
    family = catalog.release_families[0].model_copy(
        update={"calendar": [*catalog.release_families[0].calendar[:5], tbd_october]}
    )
    three_future = catalog.model_copy(update={"release_families": [family]})
    valid_horizon = evaluate_current_freshness(
        three_future,
        "US_DURABLE_GOODS_ORDERS",
        evaluated_at=datetime.fromisoformat("2026-09-07T12:00:00-04:00"),
    )
    assert valid_horizon is not None
    assert valid_horizon.maintenance_warnings == []
    assert not valid_horizon.coverage_expired


def test_authority_hash_is_deterministic_and_semantic(tmp_path: Path) -> None:
    first = load_current_freshness_catalog()
    second = load_current_freshness_catalog()
    assert first.current_freshness_hash == second.current_freshness_hash
    raw = yaml.safe_load(first.source_path.read_text(encoding="utf-8"))
    raw["release_families"][0]["calendar"][2]["official_event_id"] = "changed"
    path = tmp_path / "current_freshness.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    changed = load_current_freshness_catalog(path)
    assert changed.current_freshness_hash != first.current_freshness_hash


def test_runtime_module_has_no_live_calendar_fetch() -> None:
    source = Path("app/source_registry/current_freshness.py").read_text(encoding="utf-8")
    assert "httpx" not in source
    assert "requests" not in source
    assert "api_key" not in source.lower()


def test_current_freshness_metadata_is_absent_from_historical_authorities() -> None:
    historical_paths = [
        Path("config/features/us_macro_features.yaml"),
        Path("config/signals/us_macro_signals.yaml"),
        Path("config/pillars/us_macro_pillars.yaml"),
        Path("config/composites/us_macro_state.yaml"),
        Path("config/regimes/us_macro_regime.yaml"),
        Path("config/validation/us_macro_stage3f_v2.yaml"),
    ]
    for path in historical_paths:
        assert "current-freshness-v1" not in path.read_text(encoding="utf-8")


def test_dlcr005_creates_no_database_migration() -> None:
    assert not list(Path("migrations/versions").glob("*dlcr005*"))
    assert not list(Path("migrations/versions").glob("*current_freshness*"))
