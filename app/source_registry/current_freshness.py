"""Independent current-mode release-calendar authority and evaluation."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.source_registry.config import load_source_registry
from app.source_registry.schemas import SourceRegistryCatalog, SourceSystem

DEFAULT_CURRENT_FRESHNESS_PATH = Path("config/data_sources/current_freshness.yaml")
FULL_M3_FAMILY = "CENSUS_M3_FULL_REPORT"
FULL_M3_INDICATORS = frozenset(
    {
        "US_DURABLE_GOODS_ORDERS",
        "US_CORE_CAPITAL_GOODS_ORDERS",
    }
)


class ReleaseStatus(StrEnum):
    """Reviewable state of one official release-calendar entry."""

    SCHEDULED = "SCHEDULED"
    RESCHEDULED = "RESCHEDULED"
    RELEASED = "RELEASED"
    TBD = "TBD"
    SUSPENDED = "SUSPENDED"


class CurrentReleaseEntry(BaseModel):
    """One current-only official release-calendar fact."""

    model_config = ConfigDict(extra="forbid")

    observation_period: date
    scheduled_release_timestamp: datetime | None
    actual_release_timestamp: datetime | None = None
    status: ReleaseStatus
    official_source_url: str
    official_event_id: str | None = None
    verified_at: datetime
    previous_scheduled_timestamp: datetime | None = None
    revision_reason: str | None = None
    revision_evidence: str | None = None

    @model_validator(mode="after")
    def validate_release_evidence(self) -> CurrentReleaseEntry:
        """Reject synthetic dates and incomplete official reschedule evidence."""
        timestamps = (
            self.scheduled_release_timestamp,
            self.actual_release_timestamp,
            self.verified_at,
            self.previous_scheduled_timestamp,
        )
        if any(item is not None and item.utcoffset() is None for item in timestamps):
            raise ValueError("release calendar timestamps must be timezone-aware")
        if not self.official_source_url.startswith("https://www.census.gov/"):
            raise ValueError("Full M3 release evidence must use an official Census URL")
        if (
            self.status in {ReleaseStatus.SCHEDULED, ReleaseStatus.RESCHEDULED}
            and self.scheduled_release_timestamp is None
        ):
            raise ValueError(f"{self.status.value} requires scheduled_release_timestamp")
        if self.status is ReleaseStatus.RELEASED and (
            self.actual_release_timestamp is None and self.scheduled_release_timestamp is None
        ):
            raise ValueError("RELEASED requires an actual or scheduled release timestamp")
        if self.status in {ReleaseStatus.TBD, ReleaseStatus.SUSPENDED} and (
            self.scheduled_release_timestamp is not None
            or self.actual_release_timestamp is not None
        ):
            raise ValueError(f"{self.status.value} cannot carry a synthetic release timestamp")
        if self.status is not ReleaseStatus.RELEASED and self.actual_release_timestamp is not None:
            raise ValueError("actual_release_timestamp is only valid for RELEASED entries")
        if self.status is ReleaseStatus.RESCHEDULED and (
            self.previous_scheduled_timestamp is None
            or not self.revision_reason
            or not self.revision_evidence
        ):
            raise ValueError("RESCHEDULED requires prior timestamp, reason, and evidence")
        if self.status is not ReleaseStatus.RESCHEDULED and any(
            item is not None
            for item in (
                self.previous_scheduled_timestamp,
                self.revision_reason,
                self.revision_evidence,
            )
        ):
            raise ValueError("reschedule provenance is only valid for RESCHEDULED entries")
        if self.status is ReleaseStatus.SUSPENDED and not self.official_source_url:
            raise ValueError("SUSPENDED requires official evidence")
        return self

    @property
    def effective_release_timestamp(self) -> datetime | None:
        """Return the only timestamp allowed to activate this observation period."""
        if self.status in {ReleaseStatus.TBD, ReleaseStatus.SUSPENDED}:
            return None
        if self.status is ReleaseStatus.RELEASED and self.actual_release_timestamp is not None:
            return self.actual_release_timestamp
        return self.scheduled_release_timestamp

    def identity(self) -> str:
        """Return a stable identity for runtime audit metadata."""
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class CurrentReleaseFamily(BaseModel):
    """One explicit current-only release family and its official calendar."""

    model_config = ConfigDict(extra="forbid")

    release_family: Literal["CENSUS_M3_FULL_REPORT"]
    indicator_ids: list[str]
    timezone: Literal["America/New_York"]
    operational_grace: Literal["PT48H"]
    calendar: list[CurrentReleaseEntry]

    @model_validator(mode="after")
    def validate_family(self) -> CurrentReleaseFamily:
        """Enforce exact membership and chronological monthly period coverage."""
        if set(self.indicator_ids) != FULL_M3_INDICATORS or len(self.indicator_ids) != 2:
            raise ValueError("CENSUS_M3_FULL_REPORT membership must be the two approved indicators")
        if self.indicator_ids != sorted(self.indicator_ids):
            raise ValueError("current freshness indicator IDs must be sorted")
        if len(self.calendar) < 2:
            raise ValueError("release family requires enough entries to identify a next period")
        periods = [entry.observation_period for entry in self.calendar]
        if len(periods) != len(set(periods)):
            raise ValueError("release calendar observation periods must be unique")
        if periods != sorted(periods):
            raise ValueError("release calendar observation periods must be ordered")
        for previous, current in zip(periods, periods[1:], strict=False):
            if current != previous + relativedelta(months=1):
                raise ValueError("release calendar observation periods must be contiguous monthly")
        zone = ZoneInfo(self.timezone)
        for entry in self.calendar:
            for timestamp in (
                entry.scheduled_release_timestamp,
                entry.actual_release_timestamp,
                entry.verified_at,
                entry.previous_scheduled_timestamp,
            ):
                if (
                    timestamp is not None
                    and timestamp.utcoffset() != timestamp.astimezone(zone).utcoffset()
                ):
                    raise ValueError("release timestamps must preserve America/New_York offset")
        effective = [
            entry.effective_release_timestamp
            for entry in self.calendar
            if entry.effective_release_timestamp is not None
        ]
        if effective != sorted(effective):
            raise ValueError("effective release timestamps must be chronological")
        return self

    @property
    def grace_duration(self) -> timedelta:
        """Return the frozen absolute operational grace duration."""
        return timedelta(hours=48)


class CurrentFreshnessCatalog(BaseModel):
    """Validated independent current freshness authority."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    schema_version: str
    current_freshness_version: Literal["current-freshness-v1"]
    required_source_registry_version: Literal["stage2b5-v3"]
    required_source_registry_hash: str = Field(min_length=64, max_length=64)
    release_families: list[CurrentReleaseFamily]
    source_path: Path
    current_freshness_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_catalog(self) -> CurrentFreshnessCatalog:
        """Require the single approved Full M3 release family."""
        family_ids = [family.release_family for family in self.release_families]
        if family_ids != [FULL_M3_FAMILY]:
            raise ValueError("current-freshness-v1 requires exactly CENSUS_M3_FULL_REPORT")
        return self

    def family_for_indicator(self, indicator_id: str) -> CurrentReleaseFamily | None:
        """Return an explicitly bound family without provider or dataset inference."""
        return next(
            (family for family in self.release_families if indicator_id in family.indicator_ids),
            None,
        )


class CurrentFreshnessExpectation(BaseModel):
    """Current expected-period result with calendar audit diagnostics."""

    model_config = ConfigDict(extra="forbid")

    period: date | None
    scheduled_release_timestamp: datetime | None
    effective_release_timestamp: datetime | None
    grace_ends_at: datetime | None
    release_family: str
    calendar_entry_identity: str | None
    schedule_is_certain: bool
    coverage_expired: bool
    diagnostics: list[str] = Field(default_factory=list)
    maintenance_warnings: list[str] = Field(default_factory=list)


def load_current_freshness_catalog(
    path: Path = DEFAULT_CURRENT_FRESHNESS_PATH,
    *,
    source_registry: SourceRegistryCatalog | None = None,
) -> CurrentFreshnessCatalog:
    """Load, bind, canonicalize, and hash the current-only freshness authority."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("release_families"), list):
        raise ValueError("current freshness authority requires release_families")
    families = [CurrentReleaseFamily.model_validate(item) for item in raw["release_families"]]
    registry = source_registry or load_source_registry()
    required_version = str(raw["required_source_registry_version"])
    required_hash = str(raw["required_source_registry_hash"])
    if required_version != registry.registry_policy_version:
        raise ValueError("current freshness source registry version mismatch")
    if required_hash != registry.registry_hash:
        raise ValueError("current freshness source registry hash mismatch")
    by_indicator = registry.by_indicator()
    for family in families:
        for indicator_id in family.indicator_ids:
            policy = by_indicator.get(indicator_id)
            if policy is None or policy.primary_source is not SourceSystem.CENSUS:
                raise ValueError("current freshness family member must retain Census primary")
    hash_payload = {
        "schema_version": str(raw["schema_version"]),
        "current_freshness_version": str(raw["current_freshness_version"]),
        "required_source_registry_version": required_version,
        "required_source_registry_hash": required_hash,
        "release_families": [
            family.model_dump(mode="json")
            for family in sorted(families, key=lambda item: item.release_family)
        ],
    }
    authority_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CurrentFreshnessCatalog(
        schema_version=str(raw["schema_version"]),
        current_freshness_version=str(raw["current_freshness_version"]),
        required_source_registry_version=required_version,
        required_source_registry_hash=required_hash,
        release_families=families,
        source_path=path,
        current_freshness_hash=authority_hash,
    )


def evaluate_current_freshness(
    catalog: CurrentFreshnessCatalog,
    indicator_id: str,
    *,
    evaluated_at: datetime,
) -> CurrentFreshnessExpectation | None:
    """Evaluate one explicitly bound family without guessing missing release dates."""
    if evaluated_at.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    family = catalog.family_for_indicator(indicator_id)
    if family is None:
        return None
    instant = evaluated_at.astimezone(ZoneInfo(family.timezone))
    eligible = [
        entry
        for entry in family.calendar
        if entry.effective_release_timestamp is not None
        and entry.effective_release_timestamp <= instant
    ]
    if not eligible:
        # The reviewed current-only authority is not retroactive. Calls before its
        # first official entry continue to use the frozen registry policy.
        return None
    active = eligible[-1]
    effective = active.effective_release_timestamp
    assert effective is not None
    future = [
        entry for entry in family.calendar if entry.observation_period > active.observation_period
    ]
    expected_next_period = active.observation_period + relativedelta(months=1)
    next_entry = next(
        (entry for entry in family.calendar if entry.observation_period == expected_next_period),
        None,
    )
    diagnostics: list[str] = []
    coverage_expired = next_entry is None
    if next_entry is None:
        diagnostics.append("CURRENT_CALENDAR_COVERAGE_EXPIRED")
    elif next_entry.status is ReleaseStatus.TBD:
        diagnostics.append("CURRENT_RELEASE_DATE_TBD")
    elif next_entry.status is ReleaseStatus.SUSPENDED:
        diagnostics.append("CURRENT_RELEASE_SUSPENDED")
    warnings = ["CURRENT_FRESHNESS_CALENDAR_LOW_HORIZON"] if len(future) < 3 else []
    schedule_is_certain = not coverage_expired and not diagnostics
    return CurrentFreshnessExpectation(
        period=active.observation_period,
        scheduled_release_timestamp=active.scheduled_release_timestamp,
        effective_release_timestamp=effective,
        grace_ends_at=effective + family.grace_duration,
        release_family=family.release_family,
        calendar_entry_identity=active.identity(),
        schedule_is_certain=schedule_is_certain,
        coverage_expired=coverage_expired,
        diagnostics=diagnostics,
        maintenance_warnings=warnings,
    )
