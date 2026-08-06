"""Deterministic Stage 2 quality-rule tests."""

from datetime import date

from app.data_quality.checks import QUALITY_RULES, check_freshness, check_observation_batch
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


def test_all_15_required_quality_rules_are_registered() -> None:
    assert len(QUALITY_RULES) == 15
    assert len(set(QUALITY_RULES)) == 15
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
        "raw_response_hash_conflict",
        "duplicate_vintage",
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
