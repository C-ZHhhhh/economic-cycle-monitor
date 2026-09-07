"""Registry-driven orchestration for current primary and fallback refreshes."""

from collections.abc import Sequence
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.data_pipeline.bea_service import BeaPipelineService
from app.data_pipeline.bls_service import BlsPipelineService
from app.data_pipeline.census_service import CensusPipelineService
from app.data_pipeline.dol_service import DolPipelineService
from app.data_pipeline.fed_service import FederalReservePipelineService
from app.data_pipeline.schemas import (
    BeaSyncOptions,
    BlsSyncOptions,
    CensusSyncOptions,
    PipelineSelection,
    SourceComparisonOptions,
    SyncOptions,
    SyncSummary,
)
from app.data_pipeline.service import FredPipelineService
from app.data_pipeline.source_comparison import SourceComparisonService
from app.data_sources.bea.factory import create_bea_client
from app.data_sources.bls.factory import create_bls_client
from app.data_sources.census.factory import create_census_client
from app.data_sources.dol.factory import create_dol_client
from app.data_sources.federal_reserve.factory import create_federal_reserve_client
from app.data_sources.fred.factory import create_fred_client
from app.database.models import RawObservation
from app.database.session import db_session
from app.settings import Settings
from app.source_registry.config import load_source_registry, storage_source_name
from app.source_registry.health import indicator_source_health
from app.source_registry.readiness import validate_data_layer
from app.source_registry.schemas import (
    HealthSnapshot,
    ReadinessStatus,
    SourceRegistryCatalog,
    SourceRegistryPolicy,
    SourceSystem,
)

CurrentSourceRole = Literal["PRIMARY", "FALLBACK"]


class CurrentRefreshRequest(BaseModel):
    """A current-only indicator selection with no PIT or as-of surface."""

    model_config = ConfigDict(extra="forbid")

    indicator_ids: list[str] = Field(min_length=1)
    dry_run: bool = False

    @field_validator("indicator_ids")
    @classmethod
    def validate_indicator_ids(cls, value: list[str]) -> list[str]:
        """Reject duplicates so a source is never refreshed twice accidentally."""
        if any(not item.strip() for item in value):
            raise ValueError("indicator IDs must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("indicator IDs must be unique")
        return value


class CurrentProviderRefreshResult(BaseModel):
    """Safe, source-isolated outcome from one provider transaction."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    provider: SourceSystem
    storage_source: str
    role: CurrentSourceRole
    attempted: bool = True
    success: bool
    status: Literal["succeeded", "partial", "failed"]
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0
    latest_observation_date: date | None = None
    run_id: str | None = None
    error_type: str | None = None
    error: str | None = None


class CurrentSourceComparisonResult(BaseModel):
    """Integrity result for independently persisted primary and FRED data."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    primary_source: str
    secondary_source: Literal["fred"] = "fred"
    attempted: bool
    success: bool
    counts: dict[str, int] = Field(default_factory=dict)
    run_id: str | None = None
    error_type: str | None = None
    error: str | None = None


class CurrentIndicatorRefreshState(BaseModel):
    """Post-refresh persisted-data and primary-health snapshot."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    primary_source: SourceSystem
    fallback_source: SourceSystem | None
    primary_latest_observation_date: date | None
    fallback_latest_observation_date: date | None
    primary_health: HealthSnapshot


class CurrentRefreshResult(BaseModel):
    """Combined current refresh outcome without merging provider identities."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "partial", "failed"]
    registry_policy_version: str
    registry_hash: str
    requested_indicator_ids: list[str]
    provider_results: list[CurrentProviderRefreshResult]
    comparisons: list[CurrentSourceComparisonResult]
    indicator_states: list[CurrentIndicatorRefreshState]
    final_readiness: ReadinessStatus


_FATAL_COMPARISON_STATUSES = {
    "normalization_mismatch",
    "not_comparable",
    "seasonal_adjustment_mismatch",
    "source_value_conflict",
}


class CurrentRefreshRunner:
    """Refresh registry-declared current sources sequentially and fail closed."""

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        registry: SourceRegistryCatalog | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.registry = registry or load_source_registry()

    async def run(self, request: CurrentRefreshRequest) -> CurrentRefreshResult:
        """Refresh only requested current sources, each in its own transaction."""
        policies = self._select_policies(request.indicator_ids)
        provider_results: list[CurrentProviderRefreshResult] = []
        by_indicator_and_source: dict[tuple[str, SourceSystem], CurrentProviderRefreshResult] = {}

        for policy in policies:
            sources: list[tuple[SourceSystem, CurrentSourceRole]] = [
                (policy.primary_source, "PRIMARY")
            ]
            if policy.fallback_source is not None:
                sources.append((policy.fallback_source, "FALLBACK"))
            for source, role in sources:
                result = await self._refresh_provider(policy.indicator_id, source, role, request)
                provider_results.append(result)
                by_indicator_and_source[(policy.indicator_id, source)] = result

        comparisons = [
            self._compare_current_sources(policy, by_indicator_and_source, request.dry_run)
            for policy in policies
            if policy.fallback_source is SourceSystem.FRED_CURRENT
            and policy.primary_source is not SourceSystem.FRED_CURRENT
        ]
        states, readiness = self._inspect_final_state(policies)
        provider_successes = sum(item.success for item in provider_results)
        failures = sum(not item.success for item in provider_results) + sum(
            not item.success for item in comparisons
        )
        status: Literal["succeeded", "partial", "failed"]
        if failures == 0:
            status = "succeeded"
        elif provider_successes:
            status = "partial"
        else:
            status = "failed"
        return CurrentRefreshResult(
            status=status,
            registry_policy_version=self.registry.registry_policy_version,
            registry_hash=self.registry.registry_hash,
            requested_indicator_ids=request.indicator_ids,
            provider_results=provider_results,
            comparisons=comparisons,
            indicator_states=states,
            final_readiness=readiness,
        )

    def _select_policies(self, indicator_ids: Sequence[str]) -> list[SourceRegistryPolicy]:
        policies = self.registry.by_indicator()
        unknown = [item for item in indicator_ids if item not in policies]
        if unknown:
            raise ValueError(f"unknown source registry indicator: {unknown[0]}")
        return [policies[item] for item in indicator_ids]

    async def _refresh_provider(
        self,
        indicator_id: str,
        source: SourceSystem,
        role: CurrentSourceRole,
        request: CurrentRefreshRequest,
    ) -> CurrentProviderRefreshResult:
        storage_source = storage_source_name(source.value)
        try:
            self._require_enabled(source)
            with db_session(self.engine) as session:
                summary = await self._sync_in_session(
                    session, source, indicator_id, request.dry_run
                )
                latest = self._latest_observation_date(session, indicator_id, storage_source)
                success = (
                    summary.status == "succeeded"
                    and summary.indicator_count == 1
                    and summary.success_count == 1
                    and summary.failure_count == 0
                )
                return CurrentProviderRefreshResult(
                    indicator_id=indicator_id,
                    provider=source,
                    storage_source=storage_source,
                    role=role,
                    success=success,
                    status=summary.status,
                    inserted=summary.inserted_count,
                    revised=summary.revised_count,
                    unchanged=self._unchanged_count(summary),
                    latest_observation_date=latest,
                    run_id=str(summary.run_id),
                    error_type=(summary.errors[0].error_type if summary.errors else None),
                    error=(None if success else "Provider refresh did not fully succeed"),
                )
        except Exception as error:
            return CurrentProviderRefreshResult(
                indicator_id=indicator_id,
                provider=source,
                storage_source=storage_source,
                role=role,
                success=False,
                status="failed",
                error_type=type(error).__name__,
                error=f"Provider refresh failed ({type(error).__name__})",
            )

    async def _sync_in_session(
        self,
        session: Session,
        source: SourceSystem,
        indicator_id: str,
        dry_run: bool,
    ) -> SyncSummary:
        if source is SourceSystem.FRED_CURRENT:
            async with create_fred_client(self.settings) as client:
                return await FredPipelineService(session, self.settings, client).sync(
                    SyncOptions(indicator_id=indicator_id, dry_run=dry_run)
                )
        if source is SourceSystem.BLS:
            async with create_bls_client(self.settings) as client:
                return await BlsPipelineService(session, self.settings, client).sync(
                    BlsSyncOptions(indicator_id=indicator_id, dry_run=dry_run)
                )
        if source is SourceSystem.BEA:
            async with create_bea_client(self.settings) as client:
                return await BeaPipelineService(session, self.settings, client).sync(
                    BeaSyncOptions(indicator_id=indicator_id, dry_run=dry_run)
                )
        if source is SourceSystem.CENSUS:
            async with create_census_client(self.settings) as client:
                return await CensusPipelineService(session, self.settings, client).sync(
                    CensusSyncOptions(indicator_id=indicator_id, dry_run=dry_run)
                )
        if source is SourceSystem.FEDERAL_RESERVE:
            async with create_federal_reserve_client(self.settings) as client:
                return await FederalReservePipelineService(session, self.settings, client).sync(
                    PipelineSelection(indicator_id=indicator_id), dry_run=dry_run
                )
        if source is SourceSystem.DOL:
            async with create_dol_client(self.settings) as client:
                return await DolPipelineService(session, self.settings, client).sync(
                    PipelineSelection(indicator_id=indicator_id), dry_run=dry_run
                )
        raise ValueError("ALFRED vintage is not a current-compatible source")

    def _require_enabled(self, source: SourceSystem) -> None:
        if source is SourceSystem.ALFRED_VINTAGE:
            raise ValueError("ALFRED vintage is not a current-compatible source")
        enabled = {
            SourceSystem.FRED_CURRENT: True,
            SourceSystem.BLS: self.settings.bls_enabled,
            SourceSystem.BEA: self.settings.bea_enabled,
            SourceSystem.CENSUS: self.settings.census_enabled,
            SourceSystem.FEDERAL_RESERVE: self.settings.federal_reserve_enabled,
            SourceSystem.DOL: self.settings.dol_enabled,
        }
        if not enabled[source]:
            raise RuntimeError(f"{source.value} current source is disabled")

    def _compare_current_sources(
        self,
        policy: SourceRegistryPolicy,
        results: dict[tuple[str, SourceSystem], CurrentProviderRefreshResult],
        dry_run: bool,
    ) -> CurrentSourceComparisonResult:
        primary_storage = storage_source_name(policy.primary_source.value)
        primary = results[(policy.indicator_id, policy.primary_source)]
        fallback = results[(policy.indicator_id, SourceSystem.FRED_CURRENT)]
        if not primary.success or not fallback.success:
            return CurrentSourceComparisonResult(
                indicator_id=policy.indicator_id,
                primary_source=primary_storage,
                attempted=False,
                success=False,
                error="Comparison skipped because a provider refresh failed",
            )
        try:
            with db_session(self.engine) as session:
                summary = SourceComparisonService(session).compare(
                    SourceComparisonOptions(
                        indicator_id=policy.indicator_id,
                        primary_source=primary_storage,
                        secondary_source="fred",
                        dry_run=dry_run,
                    )
                )
                counts = self._comparison_counts(summary)
                fatal = any(counts.get(status, 0) for status in _FATAL_COMPARISON_STATUSES)
                success = (
                    summary.status == "succeeded"
                    and summary.indicator_count == 1
                    and summary.success_count == 1
                    and bool(counts)
                    and not fatal
                )
                return CurrentSourceComparisonResult(
                    indicator_id=policy.indicator_id,
                    primary_source=primary_storage,
                    attempted=True,
                    success=success,
                    counts=counts,
                    run_id=str(summary.run_id),
                    error=(None if success else "Current source integrity comparison failed"),
                )
        except Exception as error:
            return CurrentSourceComparisonResult(
                indicator_id=policy.indicator_id,
                primary_source=primary_storage,
                attempted=True,
                success=False,
                error_type=type(error).__name__,
                error=f"Current source comparison failed ({type(error).__name__})",
            )

    def _inspect_final_state(
        self, policies: Sequence[SourceRegistryPolicy]
    ) -> tuple[list[CurrentIndicatorRefreshState], ReadinessStatus]:
        with db_session(self.engine) as session:
            states = [
                CurrentIndicatorRefreshState(
                    indicator_id=policy.indicator_id,
                    primary_source=policy.primary_source,
                    fallback_source=policy.fallback_source,
                    primary_latest_observation_date=self._latest_observation_date(
                        session,
                        policy.indicator_id,
                        storage_source_name(policy.primary_source.value),
                    ),
                    fallback_latest_observation_date=(
                        self._latest_observation_date(
                            session,
                            policy.indicator_id,
                            storage_source_name(policy.fallback_source.value),
                        )
                        if policy.fallback_source is not None
                        else None
                    ),
                    primary_health=indicator_source_health(session, policy),
                )
                for policy in policies
            ]
            readiness = validate_data_layer(session, registry=self.registry)
        return states, readiness

    @staticmethod
    def _latest_observation_date(session: Session, indicator_id: str, source: str) -> date | None:
        return session.scalar(
            select(func.max(RawObservation.observation_date)).where(
                RawObservation.indicator_id == indicator_id,
                RawObservation.source == source,
            )
        )

    @staticmethod
    def _unchanged_count(summary: SyncSummary) -> int:
        return sum(
            int(result.get("unchanged", 0))
            for result in summary.results
            if isinstance(result.get("unchanged", 0), int)
        )

    @staticmethod
    def _comparison_counts(summary: SyncSummary) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in summary.results:
            raw = result.get("counts", {})
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if isinstance(key, str) and isinstance(value, int):
                        counts[key] = counts.get(key, 0) + value
        return counts
