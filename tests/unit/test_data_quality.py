"""Deterministic Stage 2 quality-rule tests."""

from datetime import date

from app.data_quality.checks import (
    QUALITY_RULES,
    check_freshness,
    check_observation_batch,
    duplicate_vintage_issue,
    pit_availability_issue,
    source_unavailable_issue,
)
from app.data_quality.scope import PITAvailabilityStatus, SafeErrorCategory
from app.data_sources.fred.schemas import FredObservation


def _observation(observation_date: str, value: str) -> FredObservation:
    return FredObservation.model_validate(
        {
            "realtime_start": "2025-01-01",
            "realtime_end": "2025-01-01",
            "date": observation_date,
            "value": value,
        }
    )


def test_all_stage_two_and_official_source_quality_rules_are_registered() -> None:
    assert len(QUALITY_RULES) == 39
    assert len(set(QUALITY_RULES)) == 39
    assert {
        "missing_value",
        "duplicate_observation",
        "invalid_numeric_value",
        "observation_date_in_future",
        "metadata_frequency_mismatch",
        "metadata_unit_mismatch",
        "metadata_seasonal_adjustment_mismatch",
        "stale_series",
        "no_recent_observations",
        "unexpected_large_revision",
        "non_monotonic_dates",
        "empty_api_response",
        "source_unavailable",
        "pit_availability_failure",
        "raw_response_hash_conflict",
        "duplicate_vintage",
        "source_mapping_missing",
        "source_metadata_mismatch",
        "source_series_unavailable",
        "source_response_partial",
        "source_business_error",
        "distribution_lag",
        "missing_in_primary_source",
        "missing_in_fallback_source",
        "normalization_mismatch",
        "seasonal_adjustment_mismatch",
        "source_value_conflict",
        "source_revision_detected",
        "duplicate_official_observation",
        "invalid_unit_multiplier",
        "unexpected_table_line",
        "unexpected_series_code",
        "unexpected_dataset",
        "unexpected_category",
        "unexpected_data_type",
        "unexpected_geography",
        "unexpected_seasonal_adjustment",
        "unexpected_unit",
        "derived_series_not_directly_comparable",
    } == set(QUALITY_RULES)


def test_observation_batch_reports_missing_duplicate_future_and_ordering() -> None:
    observations = [
        _observation("2025-02-01", "."),
        _observation("2025-01-01", "2"),
        _observation("2025-01-01", "3"),
        _observation("2027-01-01", "4"),
    ]

    issues = check_observation_batch("US_TEST", observations, today=date(2026, 1, 1))

    assert {
        "missing_value",
        "duplicate_observation",
        "non_monotonic_dates",
        "observation_date_in_future",
    } <= {issue.event_type for issue in issues}
    assert all(len(issue.fingerprint) == 64 for issue in issues)


def test_freshness_distinguishes_absent_and_stale_series() -> None:
    absent = check_freshness("US_TEST", None, max_staleness_days=30, today=date(2026, 1, 1))
    stale = check_freshness(
        "US_TEST", date(2025, 1, 1), max_staleness_days=30, today=date(2026, 1, 1)
    )

    assert absent[0].event_type == "no_recent_observations"
    assert stale[0].event_type == "stale_series"


def test_duplicate_vintage_fingerprint_preserves_source_separated_grain() -> None:
    fred = duplicate_vintage_issue(
        "US_TEST",
        observation_date=date(2025, 1, 1),
        vintage_date=date(2025, 2, 1),
        revision_number=0,
        source="fred",
    )
    official = duplicate_vintage_issue(
        "US_TEST",
        observation_date=date(2025, 1, 1),
        vintage_date=date(2025, 2, 1),
        revision_number=0,
        source="bls",
    )

    assert fred.details["source"] == "fred"
    assert official.details["source"] == "bls"
    assert fred.fingerprint != official.fingerprint


def test_pit_fingerprint_preserves_scope_date_status_and_safe_category() -> None:
    base = pit_availability_issue(
        "US_REAL_GDP",
        as_of=date(2000, 1, 1),
        status=PITAvailabilityStatus.OUTSIDE_COVERAGE,
        safe_error_category=SafeErrorCategory.COVERAGE,
        http_status=400,
        runtime_blocking=False,
    )
    repeat = pit_availability_issue(
        "US_REAL_GDP",
        as_of=date(2000, 1, 1),
        status=PITAvailabilityStatus.OUTSIDE_COVERAGE,
        safe_error_category=SafeErrorCategory.COVERAGE,
        http_status=400,
        runtime_blocking=False,
    )
    another_date = pit_availability_issue(
        "US_REAL_GDP",
        as_of=date(2000, 1, 2),
        status=PITAvailabilityStatus.OUTSIDE_COVERAGE,
        safe_error_category=SafeErrorCategory.COVERAGE,
        http_status=400,
        runtime_blocking=False,
    )
    another_status = pit_availability_issue(
        "US_REAL_GDP",
        as_of=date(2000, 1, 1),
        status=PITAvailabilityStatus.NO_VINTAGE,
        safe_error_category=SafeErrorCategory.COVERAGE,
        http_status=400,
        runtime_blocking=False,
    )
    current = source_unavailable_issue("US_REAL_GDP", RuntimeError("offline"))

    assert base.fingerprint == repeat.fingerprint
    assert len({base.fingerprint, another_date.fingerprint, another_status.fingerprint}) == 3
    assert base.fingerprint != current.fingerprint
    assert base.details["classification_version"] == "dlcr-002-v1"
