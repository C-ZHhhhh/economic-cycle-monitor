"""Typed contracts for the unified official-source registry."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data_quality.scope import RuntimeDomain


class SourceSystem(StrEnum):
    """Stable logical source identifiers used by resolution policy."""

    FRED_CURRENT = "FRED_CURRENT"
    ALFRED_VINTAGE = "ALFRED_VINTAGE"
    BLS = "BLS"
    BEA = "BEA"
    CENSUS = "CENSUS"
    FEDERAL_RESERVE = "FEDERAL_RESERVE"
    DOL = "DOL"


class MappingQuality(StrEnum):
    """Identity quality, deliberately separate from runtime availability."""

    VALID = "VALID"
    VALID_WITH_METADATA_WARNING = "VALID_WITH_METADATA_WARNING"
    PENDING_REVIEW = "PENDING_REVIEW"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class RuntimeHealth(StrEnum):
    """Current operational state for a provider or indicator/source pair."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class FallbackReason(StrEnum):
    """The only reasons permitted to activate a current-data fallback."""

    PRIMARY_BEFORE_OFFICIAL_HISTORY = "PRIMARY_BEFORE_OFFICIAL_HISTORY"
    PRIMARY_STALE_BEYOND_GRACE = "PRIMARY_STALE_BEYOND_GRACE"
    PRIMARY_TEMPORARILY_UNAVAILABLE = "PRIMARY_TEMPORARILY_UNAVAILABLE"


class CanonicalContract(BaseModel):
    """Provider-independent economic data contract."""

    model_config = ConfigDict(extra="forbid")

    frequency: Literal["weekly", "monthly", "quarterly"]
    unit: str
    seasonal_adjustment: str
    base_year: str | int | None = None


class DateSemantics(BaseModel):
    """Meaning of observation and release dates."""

    model_config = ConfigDict(extra="forbid")

    observation_date: str
    release_date: str


class HistoryPolicy(BaseModel):
    """Versioned primary/fallback coverage boundaries."""

    model_config = ConfigDict(extra="forbid")

    earliest_primary_date: date
    earliest_fallback_date: date | None
    extension_policy: Literal["PRIMARY_ONLY", "FALLBACK_BEFORE_PRIMARY_START"]
    verification_source: str
    verification_method: str


class ScheduledRelease(BaseModel):
    """One official data-period release timestamp from a fixed calendar."""

    model_config = ConfigDict(extra="forbid")

    period: date
    scheduled_release_at: datetime

    @model_validator(mode="after")
    def validate_timezone(self) -> "ScheduledRelease":
        """Reject machine-local or otherwise timezone-naive release times."""
        if self.scheduled_release_at.utcoffset() is None:
            raise ValueError("scheduled_release_at must be timezone-aware")
        return self


class FreshnessPolicy(BaseModel):
    """Expected-period freshness model and grace window."""

    model_config = ConfigDict(extra="forbid")

    schedule_type: str
    expected_lag_days: int = Field(ge=0)
    grace_days: int = Field(ge=0)
    stale_policy: Literal["DEGRADE_WITHIN_GRACE_THEN_STALE"]
    legacy_ceiling_days: int | None = Field(default=None, gt=0)
    release_timezone: str | None = None
    release_calendar: list[ScheduledRelease] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_fixed_calendar(self) -> "FreshnessPolicy":
        """Require complete, ordered evidence for fixed official calendars."""
        is_fixed = self.schedule_type.endswith("_FIXED_CALENDAR")
        if is_fixed and self.release_timezone != "America/New_York":
            raise ValueError("fixed calendars must preserve America/New_York semantics")
        if is_fixed and not self.release_calendar:
            raise ValueError("fixed calendars require official release entries")
        periods = [item.period for item in self.release_calendar]
        releases = [item.scheduled_release_at for item in self.release_calendar]
        if len(periods) != len(set(periods)):
            raise ValueError("release calendar periods must be unique")
        if periods != sorted(periods) or releases != sorted(releases):
            raise ValueError("release calendar entries must be chronological")
        if not is_fixed and (self.release_timezone or self.release_calendar):
            raise ValueError("release calendar fields require a fixed schedule type")
        return self


class ReconciliationPolicy(BaseModel):
    """One layer of a source-family revision policy."""

    model_config = ConfigDict(extra="forbid")

    strategy: str
    window_days: int | None = Field(default=None, gt=0)
    window_months: int | None = Field(default=None, gt=0)
    window_years: int | None = Field(default=None, gt=0)


class RevisionPolicy(BaseModel):
    """Recent, scheduled benchmark, and deep reconciliation rules."""

    model_config = ConfigDict(extra="forbid")

    recent_reconciliation: ReconciliationPolicy
    annual_or_benchmark_reconciliation: ReconciliationPolicy
    deep_reconciliation: ReconciliationPolicy


class DistributionLagPolicy(BaseModel):
    """Official-to-FRED distribution lag, not official release lag."""

    model_config = ConfigDict(extra="forbid")

    allowed_days: int = Field(ge=0)
    treatment: Literal["INFORMATIONAL_WITHIN_WINDOW"]


class ComparisonPolicy(BaseModel):
    """Cross-source comparison behavior, never a value-selection policy."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["EXACT", "SOURCE_ROUNDING"]
    absolute_tolerance: Decimal = Field(ge=0)
    relative_tolerance: Decimal = Field(ge=0)
    source_rounding_allowed: bool
    distribution_lag_policy: DistributionLagPolicy


class RecoveryPolicy(BaseModel):
    """Stateless recovery prerequisites before returning to primary."""

    model_config = ConfigDict(extra="forbid")

    require_valid_identity: bool
    require_expected_period: bool
    require_successful_sync: bool
    require_conflict_free_overlap: bool


class HysteresisPolicy(BaseModel):
    """Stage 2B-5 stateless hysteresis contract."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["STATELESS_MVP"]
    durable_transition_state: bool = False


class FallbackPolicy(BaseModel):
    """Allowed fallback causes and primary recovery rules."""

    model_config = ConfigDict(extra="forbid")

    allowed_reasons: list[FallbackReason]
    recovery_policy: RecoveryPolicy
    hysteresis: HysteresisPolicy


class VintagePolicy(BaseModel):
    """Date-granular vintage capability and backtest safety."""

    model_config = ConfigDict(extra="forbid")

    source: Literal[SourceSystem.ALFRED_VINTAGE]
    capability: Literal[
        "PIT_SAFE_VIA_ALFRED_PROXY",
        "ALFRED_VINTAGE_AVAILABLE_BUT_LIMITED",
        "CURRENT_SNAPSHOT_ONLY",
        "NO_VINTAGE",
        "PIT_UNVERIFIED",
    ]
    granularity: Literal["date"]
    information_set_semantics: Literal["END_OF_DAY"]
    backtest_safety: Literal[
        "PIT_SAFE_DATE_EOD",
        "PIT_LIMITED",
        "CURRENT_SNAPSHOT_ONLY",
        "UNSAFE",
        "UNVERIFIED",
    ]
    validated_as_of_dates: list[date] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence_dates(self) -> "VintagePolicy":
        """Require live evidence for safe claims and reject false evidence claims."""
        if self.backtest_safety == "PIT_SAFE_DATE_EOD" and not self.validated_as_of_dates:
            raise ValueError("PIT_SAFE_DATE_EOD requires validated_as_of_dates")
        if self.backtest_safety in {"CURRENT_SNAPSHOT_ONLY", "UNSAFE", "UNVERIFIED"} and (
            self.validated_as_of_dates
        ):
            raise ValueError(f"{self.backtest_safety} vintage policy cannot list validated dates")
        return self


class SupportingSource(BaseModel):
    """A source usable only as an input component, never as target truth."""

    model_config = ConfigDict(extra="forbid")

    source: SourceSystem
    source_identity: str
    role: Literal["SUPPORTING_COMPONENT"]


class SourceRegistryPolicy(BaseModel):
    """Complete source-resolution contract for one indicator."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    registry_policy_version: str
    primary_source: SourceSystem
    fallback_source: SourceSystem | None
    vintage_source: Literal[SourceSystem.ALFRED_VINTAGE]
    mapping_type: Literal["DIRECT", "DERIVED"]
    primary_source_identity: str
    supporting_sources: list[SupportingSource] = Field(default_factory=list)
    canonical: CanonicalContract
    date_semantics: DateSemantics
    history: HistoryPolicy
    freshness: FreshnessPolicy
    revision: RevisionPolicy
    comparison: ComparisonPolicy
    fallback: FallbackPolicy
    vintage: VintagePolicy

    @model_validator(mode="after")
    def validate_role_boundaries(self) -> "SourceRegistryPolicy":
        """Reject role confusion inside one policy."""
        if self.primary_source is SourceSystem.ALFRED_VINTAGE:
            raise ValueError("ALFRED_VINTAGE cannot be a current primary source")
        if self.fallback_source is SourceSystem.ALFRED_VINTAGE:
            raise ValueError("ALFRED_VINTAGE cannot be a current fallback source")
        if self.primary_source is SourceSystem.FRED_CURRENT and self.fallback_source is not None:
            raise ValueError("FRED_CURRENT primary cannot fall back to itself")
        if self.vintage.source is not self.vintage_source:
            raise ValueError("vintage.source must match vintage_source")
        identities = {(item.source, item.source_identity) for item in self.supporting_sources}
        if len(identities) != len(self.supporting_sources):
            raise ValueError("supporting source identities must be unique")
        if any(item.source is self.primary_source for item in self.supporting_sources):
            raise ValueError("SUPPORTING_COMPONENT cannot also be primary")
        return self


class SourceRegistryCatalog(BaseModel):
    """Validated registry and deterministic content identity."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    registry_policy_version: str
    policies: list[SourceRegistryPolicy]
    source_path: Path
    registry_hash: str

    @model_validator(mode="after")
    def validate_catalog_identity(self) -> "SourceRegistryCatalog":
        """Require unique indicator IDs and one shared policy version."""
        ids = [item.indicator_id for item in self.policies]
        if len(ids) != len(set(ids)):
            raise ValueError("source registry indicator IDs must be unique")
        if any(
            item.registry_policy_version != self.registry_policy_version for item in self.policies
        ):
            raise ValueError("every policy must use the catalog registry_policy_version")
        return self

    def by_indicator(self) -> dict[str, SourceRegistryPolicy]:
        """Return policies keyed by stable indicator ID."""
        return {item.indicator_id: item for item in self.policies}


class EffectiveObservation(BaseModel):
    """Canonical source-resolved observation contract for future consumers."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    observation_date: date
    value: Decimal
    unit: str
    frequency: str
    source_used: str
    source_identity: str
    source_policy_version: str
    registry_hash: str
    mapping_version: str
    is_fallback: bool
    resolution_reason: str
    mapping_quality: MappingQuality
    provider_health: RuntimeHealth
    indicator_source_health: RuntimeHealth
    release_date: date | None
    retrieved_at: datetime | None
    vintage_as_of: date | None
    vintage_source: str | None
    is_point_in_time_safe: bool


class HealthSnapshot(BaseModel):
    """Computed runtime health without writing policy into the registry."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str | None = None
    source: str
    runtime_domain: RuntimeDomain | None = None
    mapping_quality: MappingQuality
    runtime_health: RuntimeHealth
    expected_latest_period: date | None = None
    scheduled_release_at: datetime | None = None
    grace_ends_at: datetime | None = None
    schedule_is_certain: bool = True
    latest_observation_date: date | None = None
    latest_success_at: datetime | None = None
    reasons: list[str] = Field(default_factory=list)


class ReadinessStatus(BaseModel):
    """Independent current-data and point-in-time readiness gates."""

    model_config = ConfigDict(extra="forbid")

    registry_policy_version: str
    registry_hash: str
    current_data_ready: bool
    pit_contract_ready: bool
    pit_runtime_ready: bool
    pit_runtime_health: RuntimeHealth
    pit_backtest_ready: bool
    current_blockers: list[str] = Field(default_factory=list)
    pit_blockers: list[str] = Field(default_factory=list)
    pit_contract_blockers: list[str] = Field(default_factory=list)
    pit_runtime_blockers: list[str] = Field(default_factory=list)
    degraded_nonblocking: list[str] = Field(default_factory=list)
