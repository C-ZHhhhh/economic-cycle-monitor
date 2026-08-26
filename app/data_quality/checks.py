"""Stage 2 data-quality rules; no economic scoring or feature calculation."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data_sources.fred.metadata import MetadataValidationResult
from app.data_sources.fred.schemas import FredObservation
from app.data_sources.official.schemas import OfficialObservation

Severity = Literal["info", "warning", "error", "critical"]

QUALITY_RULES = (
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
)


class QualityIssue(BaseModel):
    """One deterministic and persistable data-quality finding."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str | None
    event_type: str
    severity: Severity
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str = ""

    @model_validator(mode="after")
    def add_fingerprint(self) -> "QualityIssue":
        if not self.fingerprint:
            material = json.dumps(
                [self.indicator_id, self.event_type, self.details],
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            self.fingerprint = hashlib.sha256(material.encode()).hexdigest()
        return self


def check_observation_batch(
    indicator_id: str, observations: list[FredObservation], *, today: date | None = None
) -> list[QualityIssue]:
    """Validate raw ordering, presence, values, and dates before persistence."""
    current_date = today or datetime.now(UTC).date()
    if not observations:
        return [
            QualityIssue(
                indicator_id=indicator_id,
                event_type="empty_api_response",
                severity="error",
                message="FRED returned no observations",
            )
        ]
    issues: list[QualityIssue] = []
    seen: set[date] = set()
    previous_date: date | None = None
    for item in observations:
        if item.value is None:
            issues.append(
                QualityIssue(
                    indicator_id=indicator_id,
                    event_type="missing_value",
                    severity="warning",
                    message="FRED returned a missing observation value",
                    details={"observation_date": item.date},
                )
            )
        elif not item.value.is_finite():
            issues.append(
                QualityIssue(
                    indicator_id=indicator_id,
                    event_type="invalid_numeric_value",
                    severity="error",
                    message="Observation value is not finite",
                    details={"observation_date": item.date, "value": str(item.value)},
                )
            )
        if item.date in seen:
            issues.append(
                QualityIssue(
                    indicator_id=indicator_id,
                    event_type="duplicate_observation",
                    severity="error",
                    message="Response contains the same observation date more than once",
                    details={"observation_date": item.date},
                )
            )
        if previous_date is not None and item.date < previous_date:
            issues.append(
                QualityIssue(
                    indicator_id=indicator_id,
                    event_type="non_monotonic_dates",
                    severity="error",
                    message="Response observation dates are not monotonic",
                    details={"previous_date": previous_date, "observation_date": item.date},
                )
            )
        if item.date > current_date:
            issues.append(
                QualityIssue(
                    indicator_id=indicator_id,
                    event_type="observation_date_in_future",
                    severity="error",
                    message="Observation date is in the future",
                    details={"observation_date": item.date, "today": current_date},
                )
            )
        seen.add(item.date)
        previous_date = item.date
    return issues


def check_metadata_validation(
    indicator_id: str, result: MetadataValidationResult
) -> list[QualityIssue]:
    """Map validation messages to the three required metadata rule types."""
    issues: list[QualityIssue] = []
    mappings = {
        "frequency": "metadata_frequency_mismatch",
        "units": "metadata_unit_mismatch",
        "seasonal adjustment": "metadata_seasonal_adjustment_mismatch",
    }
    for message in [*result.errors, *result.warnings]:
        event_type = next(
            (rule for prefix, rule in mappings.items() if message.startswith(prefix)), None
        )
        if event_type is not None:
            issues.append(
                QualityIssue(
                    indicator_id=indicator_id,
                    event_type=event_type,
                    severity="error" if message in result.errors else "warning",
                    message=message,
                )
            )
    return issues


def check_freshness(
    indicator_id: str,
    latest_date: date | None,
    *,
    max_staleness_days: int,
    today: date | None = None,
) -> list[QualityIssue]:
    """Detect absent or stale current observations."""
    current_date = today or datetime.now(UTC).date()
    if latest_date is None:
        return [
            QualityIssue(
                indicator_id=indicator_id,
                event_type="no_recent_observations",
                severity="error",
                message="Indicator has no current observations",
            )
        ]
    age = (current_date - latest_date).days
    if age > max_staleness_days:
        return [
            QualityIssue(
                indicator_id=indicator_id,
                event_type="stale_series",
                severity="warning",
                message="Latest observation exceeds the configured staleness threshold",
                details={"latest_date": latest_date, "age_days": age},
            )
        ]
    return []


def check_revision(
    indicator_id: str,
    *,
    previous_value: Decimal,
    current_value: Decimal,
    observation_date: date,
    threshold_ratio: Decimal = Decimal("0.5"),
) -> list[QualityIssue]:
    """Flag unusually large value revisions using a transparent fixed threshold."""
    denominator = abs(previous_value)
    ratio = (
        abs(current_value - previous_value) / denominator if denominator else Decimal("Infinity")
    )
    if ratio <= threshold_ratio:
        return []
    return [
        QualityIssue(
            indicator_id=indicator_id,
            event_type="unexpected_large_revision",
            severity="warning",
            message="Revision exceeds the Stage 2 quality threshold",
            details={
                "observation_date": observation_date,
                "previous_value": str(previous_value),
                "current_value": str(current_value),
                "ratio": str(ratio),
            },
        )
    ]


def source_unavailable_issue(indicator_id: str, error: Exception) -> QualityIssue:
    """Create a safe source failure finding."""
    return QualityIssue(
        indicator_id=indicator_id,
        event_type="source_unavailable",
        severity="error",
        message=f"FRED request failed: {type(error).__name__}",
    )


def check_official_observation_batch(
    indicator_id: str,
    observations: list[OfficialObservation],
    *,
    source: str,
    today: date | None = None,
) -> list[QualityIssue]:
    """Validate provider-neutral observations without assuming FRED semantics."""
    current_date = today or datetime.now(UTC).date()
    if not observations:
        return [
            QualityIssue(
                indicator_id=indicator_id,
                event_type="source_series_unavailable",
                severity="warning",
                message=f"{source.upper()} returned no observations for the requested series",
                details={"source": source},
            )
        ]
    issues: list[QualityIssue] = []
    seen: set[date] = set()
    previous: date | None = None
    for item in observations:
        if item.value is None:
            issues.append(
                QualityIssue(
                    indicator_id=indicator_id,
                    event_type="missing_value",
                    severity="warning",
                    message=f"{source.upper()} returned a missing observation value",
                    details={"source": source, "observation_date": item.date},
                )
            )
        if item.date in seen:
            issues.append(
                QualityIssue(
                    indicator_id=indicator_id,
                    event_type="duplicate_official_observation",
                    severity="error",
                    message="Official-source response contains a duplicate observation date",
                    details={"source": source, "observation_date": item.date},
                )
            )
        if previous is not None and item.date < previous:
            issues.append(
                QualityIssue(
                    indicator_id=indicator_id,
                    event_type="non_monotonic_dates",
                    severity="error",
                    message="Official-source observations are not monotonic",
                    details={"source": source, "observation_date": item.date},
                )
            )
        if item.date > current_date:
            issues.append(
                QualityIssue(
                    indicator_id=indicator_id,
                    event_type="observation_date_in_future",
                    severity="error",
                    message="Official-source observation date is in the future",
                    details={"source": source, "observation_date": item.date},
                )
            )
        seen.add(item.date)
        previous = item.date
    return issues


def official_source_issue(
    indicator_id: str | None,
    *,
    event_type: str,
    severity: Severity,
    source: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> QualityIssue:
    """Create a source-aware finding with stable, credential-free details."""
    return QualityIssue(
        indicator_id=indicator_id,
        event_type=event_type,
        severity=severity,
        message=message,
        details={"source": source, **(details or {})},
    )


def raw_hash_conflict_issue(
    indicator_id: str | None, *, response_path: str, hashes: list[str]
) -> QualityIssue:
    """Represent conflicting bytes claimed by one raw response path."""
    return QualityIssue(
        indicator_id=indicator_id,
        event_type="raw_response_hash_conflict",
        severity="critical",
        message="One raw response path is associated with multiple hashes",
        details={"response_path": response_path, "hashes": sorted(hashes)},
    )


def duplicate_vintage_issue(
    indicator_id: str,
    *,
    observation_date: date,
    vintage_date: date,
    revision_number: int,
    source: str,
) -> QualityIssue:
    """Represent duplicate rows at the source-separated vintage grain."""
    return QualityIssue(
        indicator_id=indicator_id,
        event_type="duplicate_vintage",
        severity="error",
        message="Duplicate observation vintage detected",
        details={
            "observation_date": observation_date,
            "vintage_date": vintage_date,
            "revision_number": revision_number,
            "source": source,
        },
    )
