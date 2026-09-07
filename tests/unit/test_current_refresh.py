"""Offline contract tests for registry-driven current refresh orchestration."""

from collections.abc import Sequence
from datetime import date
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.data_pipeline.current_refresh import (
    CurrentIndicatorRefreshState,
    CurrentProviderRefreshResult,
    CurrentRefreshRequest,
    CurrentRefreshRunner,
    CurrentSourceComparisonResult,
    CurrentSourceRole,
)
from app.source_registry.config import load_source_registry, storage_source_name
from app.source_registry.schemas import (
    HealthSnapshot,
    MappingQuality,
    ReadinessStatus,
    RuntimeHealth,
    SourceRegistryPolicy,
    SourceSystem,
)

ISSUE17_INDICATORS = [
    "US_DURABLE_GOODS_ORDERS",
    "US_CORE_CAPITAL_GOODS_ORDERS",
]


def _readiness(*, current: bool = True) -> ReadinessStatus:
    registry = load_source_registry()
    return ReadinessStatus(
        registry_policy_version=registry.registry_policy_version,
        registry_hash=registry.registry_hash,
        current_data_ready=current,
        pit_contract_ready=True,
        pit_runtime_ready=True,
        pit_runtime_health=RuntimeHealth.HEALTHY,
        pit_backtest_ready=True,
        current_blockers=[] if current else ["RUNTIME_STALE:test"],
    )


def _health(indicator_id: str, source: str) -> HealthSnapshot:
    return HealthSnapshot(
        indicator_id=indicator_id,
        source=source,
        mapping_quality=MappingQuality.VALID,
        runtime_health=RuntimeHealth.HEALTHY,
        expected_latest_period=date(2026, 7, 1),
        latest_observation_date=date(2026, 7, 1),
    )


class StubCurrentRefreshRunner(CurrentRefreshRunner):
    """Exercise orchestration policy without constructing clients or using a DB."""

    def __init__(
        self,
        *,
        failed_sources: set[SourceSystem] | None = None,
        comparison_success: bool = True,
        current_ready: bool = True,
    ) -> None:
        self.registry = load_source_registry()
        self.failed_sources = failed_sources or set()
        self.comparison_success = comparison_success
        self.current_ready = current_ready
        self.calls: list[tuple[str, SourceSystem, CurrentSourceRole]] = []

    async def _refresh_provider(
        self,
        indicator_id: str,
        source: SourceSystem,
        role: CurrentSourceRole,
        request: CurrentRefreshRequest,
    ) -> CurrentProviderRefreshResult:
        del request
        self.calls.append((indicator_id, source, role))
        success = source not in self.failed_sources
        return CurrentProviderRefreshResult(
            indicator_id=indicator_id,
            provider=source,
            storage_source=storage_source_name(source.value),
            role=role,
            success=success,
            status="succeeded" if success else "failed",
            inserted=int(success),
            latest_observation_date=date(2026, 7, 1) if success else None,
            error=None if success else "safe failure",
        )

    def _compare_current_sources(
        self,
        policy: SourceRegistryPolicy,
        results: dict[tuple[str, SourceSystem], CurrentProviderRefreshResult],
        dry_run: bool,
    ) -> CurrentSourceComparisonResult:
        del dry_run
        primary = results[(policy.indicator_id, policy.primary_source)]
        fallback = results[(policy.indicator_id, SourceSystem.FRED_CURRENT)]
        attempted = primary.success and fallback.success
        success = attempted and self.comparison_success
        return CurrentSourceComparisonResult(
            indicator_id=policy.indicator_id,
            primary_source=storage_source_name(policy.primary_source.value),
            attempted=attempted,
            success=success,
            counts={"match": 319} if success else {"source_value_conflict": 1},
            error=None if success else "safe comparison failure",
        )

    def _inspect_final_state(
        self, policies: Sequence[SourceRegistryPolicy]
    ) -> tuple[list[CurrentIndicatorRefreshState], ReadinessStatus]:
        states = [
            CurrentIndicatorRefreshState(
                indicator_id=policy.indicator_id,
                primary_source=policy.primary_source,
                fallback_source=policy.fallback_source,
                primary_latest_observation_date=date(2026, 7, 1),
                fallback_latest_observation_date=date(2026, 7, 1),
                primary_health=_health(
                    policy.indicator_id, storage_source_name(policy.primary_source.value)
                ),
            )
            for policy in policies
        ]
        return states, _readiness(current=self.current_ready)


@pytest.mark.asyncio
async def test_targeted_census_refresh_also_schedules_registry_fred_fallback() -> None:
    runner = StubCurrentRefreshRunner()
    result = await runner.run(CurrentRefreshRequest(indicator_ids=[ISSUE17_INDICATORS[0]]))

    assert runner.calls == [
        (ISSUE17_INDICATORS[0], SourceSystem.CENSUS, "PRIMARY"),
        (ISSUE17_INDICATORS[0], SourceSystem.FRED_CURRENT, "FALLBACK"),
    ]
    assert result.status == "succeeded"
    assert result.comparisons[0].counts == {"match": 319}


@pytest.mark.asyncio
async def test_only_requested_indicator_ids_are_refreshed() -> None:
    runner = StubCurrentRefreshRunner()
    result = await runner.run(CurrentRefreshRequest(indicator_ids=[ISSUE17_INDICATORS[1]]))

    assert result.requested_indicator_ids == [ISSUE17_INDICATORS[1]]
    assert {item[0] for item in runner.calls} == {ISSUE17_INDICATORS[1]}


@pytest.mark.asyncio
async def test_both_issue17_indicators_schedule_independent_primary_and_fallback() -> None:
    runner = StubCurrentRefreshRunner()
    result = await runner.run(CurrentRefreshRequest(indicator_ids=ISSUE17_INDICATORS))

    assert len(result.provider_results) == 4
    assert len(result.comparisons) == 2
    assert all(item.provider is not SourceSystem.ALFRED_VINTAGE for item in result.provider_results)
    assert all(item.storage_source in {"census", "fred"} for item in result.provider_results)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [SourceSystem.CENSUS, SourceSystem.FRED_CURRENT])
async def test_partial_provider_failure_preserves_successful_provider_result(
    failed: SourceSystem,
) -> None:
    runner = StubCurrentRefreshRunner(failed_sources={failed})
    result = await runner.run(CurrentRefreshRequest(indicator_ids=[ISSUE17_INDICATORS[0]]))

    assert result.status == "partial"
    assert sum(item.success for item in result.provider_results) == 1
    assert sum(not item.success for item in result.provider_results) == 1
    assert not result.comparisons[0].attempted


@pytest.mark.asyncio
async def test_comparison_conflict_fails_closed_without_discarding_provider_results() -> None:
    runner = StubCurrentRefreshRunner(comparison_success=False)
    result = await runner.run(CurrentRefreshRequest(indicator_ids=[ISSUE17_INDICATORS[0]]))

    assert result.status == "partial"
    assert all(item.success for item in result.provider_results)
    assert result.comparisons[0].counts == {"source_value_conflict": 1}


@pytest.mark.asyncio
async def test_primary_stale_readiness_remains_blocked_even_after_fallback_refresh() -> None:
    runner = StubCurrentRefreshRunner(current_ready=False)
    result = await runner.run(CurrentRefreshRequest(indicator_ids=[ISSUE17_INDICATORS[0]]))

    assert result.status == "succeeded"
    assert not result.final_readiness.current_data_ready
    assert result.final_readiness.pit_backtest_ready


def test_request_rejects_pit_as_of_surface() -> None:
    with pytest.raises(ValidationError, match="as_of"):
        CurrentRefreshRequest.model_validate(
            {"indicator_ids": ISSUE17_INDICATORS, "as_of": "2020-01-01"}
        )


def test_request_rejects_duplicate_indicator_ids() -> None:
    with pytest.raises(ValidationError, match="unique"):
        CurrentRefreshRequest(indicator_ids=[ISSUE17_INDICATORS[0]] * 2)


def test_unknown_indicator_fails_before_any_provider_is_called() -> None:
    runner = StubCurrentRefreshRunner()
    with pytest.raises(ValueError, match="unknown source registry indicator"):
        runner._select_policies(["NOT_AN_INDICATOR"])
    assert runner.calls == []


def test_alfred_is_rejected_as_a_current_source() -> None:
    runner = StubCurrentRefreshRunner()
    runner.settings = cast(Any, object())
    with pytest.raises(ValueError, match="not a current-compatible source"):
        runner._require_enabled(SourceSystem.ALFRED_VINTAGE)
