"""PostgreSQL transaction-isolation tests for current refresh orchestration."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.data_pipeline.current_refresh import (
    CurrentIndicatorRefreshState,
    CurrentProviderRefreshResult,
    CurrentRefreshRequest,
    CurrentRefreshRunner,
    CurrentSourceComparisonResult,
)
from app.data_pipeline.schemas import SyncSummary
from app.database.models import RawObservation
from app.database.session import db_session
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from app.settings import Settings
from app.source_registry.config import load_source_registry, storage_source_name
from app.source_registry.schemas import (
    HealthSnapshot,
    MappingQuality,
    ReadinessStatus,
    RuntimeHealth,
    SourceRegistryPolicy,
    SourceSystem,
)

pytestmark = pytest.mark.integration

INDICATOR_ID = "US_DURABLE_GOODS_ORDERS"
TEST_DATE = date(2098, 7, 1)


class TransactionFixtureRunner(CurrentRefreshRunner):
    """Persist fixture rows through the production per-provider transaction boundary."""

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        failed_source: SourceSystem,
    ) -> None:
        super().__init__(engine, settings)
        self.failed_source = failed_source

    async def _sync_in_session(
        self,
        session: Session,
        source: SourceSystem,
        indicator_id: str,
        dry_run: bool,
    ) -> SyncSummary:
        assert not dry_run
        storage_source = storage_source_name(source.value)
        session.add(
            RawObservation(
                indicator_id=indicator_id,
                observation_date=TEST_DATE,
                value=Decimal("1"),
                source=storage_source,
                retrieved_at=datetime(2098, 9, 1, tzinfo=UTC),
                raw_payload_hash=(storage_source[0] * 64),
                source_details_json={"fixture": True},
            )
        )
        session.flush()
        if source is self.failed_source:
            raise RuntimeError("offline fixture failure")
        return SyncSummary(
            run_id=uuid4(),
            operation=f"fixture_{storage_source}",
            status="succeeded",
            indicator_count=1,
            success_count=1,
            inserted_count=1,
            results=[{"indicator_id": indicator_id, "inserted": 1, "unchanged": 0}],
        )

    def _compare_current_sources(
        self,
        policy: SourceRegistryPolicy,
        results: dict[tuple[str, SourceSystem], CurrentProviderRefreshResult],
        dry_run: bool,
    ) -> CurrentSourceComparisonResult:
        del results, dry_run
        return CurrentSourceComparisonResult(
            indicator_id=policy.indicator_id,
            primary_source="census",
            attempted=False,
            success=False,
            error="fixture provider failure",
        )

    def _inspect_final_state(
        self, policies: Sequence[SourceRegistryPolicy]
    ) -> tuple[list[CurrentIndicatorRefreshState], ReadinessStatus]:
        policy = policies[0]
        health = HealthSnapshot(
            indicator_id=policy.indicator_id,
            source="census",
            mapping_quality=MappingQuality.VALID,
            runtime_health=RuntimeHealth.STALE,
        )
        readiness = ReadinessStatus(
            registry_policy_version=self.registry.registry_policy_version,
            registry_hash=self.registry.registry_hash,
            current_data_ready=False,
            pit_contract_ready=True,
            pit_runtime_ready=True,
            pit_runtime_health=RuntimeHealth.HEALTHY,
            pit_backtest_ready=True,
            current_blockers=[f"RUNTIME_STALE:{policy.indicator_id}"],
        )
        return (
            [
                CurrentIndicatorRefreshState(
                    indicator_id=policy.indicator_id,
                    primary_source=policy.primary_source,
                    fallback_source=policy.fallback_source,
                    primary_latest_observation_date=None,
                    fallback_latest_observation_date=None,
                    primary_health=health,
                )
            ],
            readiness,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_source", "persisted_source"),
    [
        (SourceSystem.CENSUS, "fred"),
        (SourceSystem.FRED_CURRENT, "census"),
    ],
)
async def test_provider_failure_rolls_back_only_its_own_transaction(
    postgres_engine: Engine,
    settings: Settings,
    failed_source: SourceSystem,
    persisted_source: str,
) -> None:
    registry = load_source_registry()
    with db_session(postgres_engine) as session:
        seed_indicator_catalog(session, load_indicator_catalog(), registry)
        session.execute(
            delete(RawObservation).where(
                RawObservation.indicator_id == INDICATOR_ID,
                RawObservation.observation_date == TEST_DATE,
                RawObservation.source.in_(["census", "fred"]),
            )
        )
    runtime_settings = settings.model_copy(
        update={"database_url": str(postgres_engine.url), "census_enabled": True}
    )
    try:
        result = await TransactionFixtureRunner(
            postgres_engine, runtime_settings, failed_source=failed_source
        ).run(CurrentRefreshRequest(indicator_ids=[INDICATOR_ID]))
        with db_session(postgres_engine) as session:
            rows = list(
                session.scalars(
                    select(RawObservation).where(
                        RawObservation.indicator_id == INDICATOR_ID,
                        RawObservation.observation_date == TEST_DATE,
                        RawObservation.source.in_(["census", "fred"]),
                    )
                ).all()
            )
        assert result.status == "partial"
        assert [(row.source, row.value) for row in rows] == [(persisted_source, Decimal("1"))]
        assert result.final_readiness.pit_backtest_ready
    finally:
        with db_session(postgres_engine) as session:
            session.execute(
                delete(RawObservation).where(
                    RawObservation.indicator_id == INDICATOR_ID,
                    RawObservation.observation_date == TEST_DATE,
                    RawObservation.source.in_(["census", "fred"]),
                )
            )
