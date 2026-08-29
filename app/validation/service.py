"""Bounded Stage 3F pilot orchestration over the production Stage 2-to-3E path."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.composites.schemas import HistoricalCoverageState, MacroStateStatus
from app.composites.service import MacroStateEngine
from app.data_sources.fred.client import FredClient
from app.data_sources.fred.factory import create_fred_client
from app.database.models import IndicatorRegistry, ObservationVintage
from app.features.schemas import FeatureResult, FeatureStatus
from app.features.service import FeatureEngine
from app.pillars.schemas import PillarStatus
from app.pillars.service import PillarEngine
from app.regimes.schemas import RegimeResult, RegimeStatus, TrajectoryStatus
from app.regimes.service import MacroRegimeEngine
from app.settings import Settings
from app.signals.schemas import NormalizedSignalResult, SignalStatus
from app.signals.service import SignalEngine
from app.source_registry.pit import PITSnapshotFetchFailed, get_effective_observations_on_demand
from app.source_registry.readiness import validate_data_layer
from app.validation.artifacts import ArtifactWriter, artifact_hashes, storage_snapshot
from app.validation.budget import BudgetStop, BudgetTracker
from app.validation.checkpoint import write_checkpoint
from app.validation.config import load_validation_catalog
from app.validation.coverage import discover_boundaries
from app.validation.hashing import semantic_hash
from app.validation.metrics import (
    calendar_duration_occupancy,
    event_count_occupancy,
    regime_durations,
    transition_counts,
)
from app.validation.planner import build_pilot_plan, estimate_full_run, propose_pilot_window
from app.validation.quality import contains_credential, future_dates, regime_quality_evidence
from app.validation.replay import history_run_hash
from app.validation.schedule import (
    build_release_event,
    combined_schedule_hash,
    group_candidates,
    release_schedule,
)
from app.validation.schemas import (
    BudgetSnapshot,
    Checkpoint,
    CoverageSnapshot,
    EvaluationSchedule,
    MaterializationPlan,
    PilotSummary,
    QualityEvidence,
    RegimeHistoryRow,
    ReleaseCandidate,
    ReleaseEvent,
    SemanticInformationSet,
    StopReason,
    UniverseMode,
    ValidationCadence,
    ValidationCatalog,
    ValidationPhase,
    ValidationStatus,
)


class HistoricalValidationError(RuntimeError):
    """A Stage 3F validation gate could not complete safely."""


class PilotQualityFailure(HistoricalValidationError):
    """A hard quality gate stopped the pilot."""


def _latest_by_id(
    rows: Sequence[FeatureResult] | Sequence[NormalizedSignalResult],
    *,
    id_field: str,
) -> dict[str, FeatureResult | NormalizedSignalResult]:
    latest: dict[str, FeatureResult | NormalizedSignalResult] = {}
    for row in rows:
        key = str(getattr(row, id_field))
        previous = latest.get(key)
        if previous is None or row.feature_observation_date > previous.feature_observation_date:
            latest[key] = row
    return latest


def _history_row(
    result: RegimeResult,
    *,
    universe_mode: UniverseMode,
    cadence: ValidationCadence,
    validation_status: str,
) -> RegimeHistoryRow:
    return RegimeHistoryRow(
        universe_mode=universe_mode,
        cadence=cadence,
        evaluation_as_of=result.evaluation_as_of,
        growth_strength=result.growth_strength,
        inflation_pressure=result.inflation_pressure,
        growth_trajectory_delta=result.growth_trajectory.delta,
        inflation_trajectory_delta=result.inflation_trajectory.delta,
        base_regime_id=result.base_regime_id,
        growth_condition=result.growth_condition.value if result.growth_condition else None,
        inflation_condition=(
            result.inflation_condition.value if result.inflation_condition else None
        ),
        growth_trajectory=(
            result.growth_trajectory.qualifier.value if result.growth_trajectory.qualifier else None
        ),
        inflation_trajectory=(
            result.inflation_trajectory.qualifier.value
            if result.inflation_trajectory.qualifier
            else None
        ),
        minimum_axis_data_confidence=result.minimum_axis_data_confidence,
        regime_status=result.regime_status.value,
        is_point_in_time_safe=result.is_point_in_time_safe,
        regime_calculation_hash=result.calculation_hash,
        macro_state_calculation_hash=result.macro_state_calculation_hash,
        semantic_validation_status=validation_status,
    )


class HistoricalValidationService:
    """Run only the approved framework coverage audit and bounded pilot."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        catalog: ValidationCatalog | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.catalog = catalog or load_validation_catalog()
        self.writer = ArtifactWriter(self.catalog.artifact_root)
        self._completed_units: list[str] = []
        self._failed_units: dict[str, str] = {}
        self._artifact_paths: list[Path] = []
        self._schedule_hash = "DISCOVERY_PENDING"
        self._stop_reason: StopReason | None = None
        self._tracker: BudgetTracker | None = None
        self._requests_by_indicator: Counter[str] = Counter()
        self._requests_by_endpoint: Counter[str] = Counter()
        self._series_to_indicator: dict[str, str] = {}

    def _checkpoint(self, reason: StopReason | None) -> None:
        tracker = self._tracker
        counters = (
            tracker.snapshot()
            if tracker is not None
            else BudgetSnapshot(
                logical_pit_units=0,
                completed_logical_units=0,
                external_alfred_http_requests=0,
                retry_count=0,
                cache_reuse_count=0,
                incremental_storage_bytes=0,
                elapsed_wall_seconds=0,
            )
        )
        checkpoint = Checkpoint(
            validation_spec_hash=self.catalog.specification_hash,
            production_authority_hashes=self.catalog.authorities.ordered_hashes(),
            schedule_hash=self._schedule_hash,
            universe_mode=UniverseMode.FULL_FIXED_UNIVERSE,
            completed_logical_units=self._completed_units,
            failed_units=self._failed_units,
            counters=counters,
            artifact_hashes=artifact_hashes(self._artifact_paths),
            stop_reason=reason,
        )
        write_checkpoint(self.catalog.checkpoint_path, checkpoint)
        self._stop_reason = reason

    def _attempt_guard(
        self, endpoint: str, safe_parameters: dict[str, str], attempt_number: int
    ) -> None:
        tracker = self._require_tracker()
        tracker.before_http_attempt(attempt_number=attempt_number)
        series_id = safe_parameters.get("series_id")
        if series_id is not None:
            indicator_id = self._series_to_indicator.get(series_id, "UNKNOWN")
            self._requests_by_indicator[indicator_id] += 1
        self._requests_by_endpoint[endpoint] += 1

    def _require_tracker(self) -> BudgetTracker:
        if self._tracker is None:
            raise HistoricalValidationError("PILOT_BUDGET_TRACKER_NOT_INITIALIZED")
        return self._tracker

    def _indicators(self) -> list[IndicatorRegistry]:
        rows = list(
            self.session.scalars(
                select(IndicatorRegistry)
                .where(IndicatorRegistry.enabled.is_(True))
                .order_by(IndicatorRegistry.indicator_id)
            ).all()
        )
        expected = {item.indicator_id for item in self.catalog_source_policies()}
        by_id = {item.indicator_id: item for item in rows}
        missing = sorted(expected - set(by_id))
        if missing:
            raise HistoricalValidationError(f"INDICATOR_NOT_MATERIALIZED:{missing[0]}")
        selected = [by_id[indicator_id] for indicator_id in sorted(expected)]
        if len(selected) != 33 or any(item.fred_series_id is None for item in selected):
            raise HistoricalValidationError("STAGE3F_REQUIRES_33_FRED_IDENTITIES")
        return selected

    def catalog_source_policies(self) -> list[Any]:
        """Return frozen source policies through the Stage 3F authority binding."""
        from app.source_registry.config import load_source_registry

        registry = load_source_registry()
        if registry.registry_hash != self.catalog.authorities.source_registry_hash:
            raise HistoricalValidationError("PRODUCTION_AUTHORITY_DRIFT")
        return registry.policies

    async def _discover_candidates(
        self, client: FredClient, indicators: Sequence[IndicatorRegistry]
    ) -> tuple[list[ReleaseCandidate], dict[str, list[date]]]:
        tracker = self._require_tracker()
        policies = {item.indicator_id: item for item in self.catalog_source_policies()}
        cutoff = date.today() - timedelta(days=self.catalog.pilot_planner.discovery_lookback_days)
        candidates: list[ReleaseCandidate] = []
        all_dates: dict[str, list[date]] = {}
        for indicator in indicators:
            assert indicator.fred_series_id is not None
            tracker.begin_logical_unit()
            before = tracker.external_alfred_http_requests
            dates, payload = await client.get_vintage_dates(indicator.fred_series_id)
            all_dates[indicator.indicator_id] = dates
            policy = policies[indicator.indicator_id]
            candidates.extend(
                ReleaseCandidate(
                    event_date=item,
                    indicator_id=indicator.indicator_id,
                    frequency=policy.canonical.frequency,
                    source_family=policy.primary_source.value,
                )
                for item in dates
                if cutoff <= item <= date.today()
            )
            tracker.complete_logical_unit(
                reused=payload.from_cache or tracker.external_alfred_http_requests == before
            )
        return candidates, all_dates

    def _snapshot_exists(self, indicator_id: str, as_of: date) -> bool:
        return (
            self.session.scalar(
                select(ObservationVintage.id)
                .where(
                    ObservationVintage.indicator_id == indicator_id,
                    ObservationVintage.source == "fred",
                    ObservationVintage.vintage_date == as_of,
                    ObservationVintage.release_stage == "as_known_on",
                )
                .limit(1)
            )
            is not None
        )

    async def _materialize_snapshot(
        self, client: FredClient, *, indicator_id: str, as_of: date, unit_id: str
    ) -> None:
        if unit_id in self._completed_units:
            return
        tracker = self._require_tracker()
        tracker.begin_logical_unit()
        was_cached = self._snapshot_exists(indicator_id, as_of)
        before = tracker.external_alfred_http_requests
        try:
            await get_effective_observations_on_demand(
                self.session,
                indicator_id,
                as_of,
                as_of,
                as_of,
                settings=self.settings,
                client=client,
            )
        except PITSnapshotFetchFailed as error:
            self._failed_units[unit_id] = error.availability_status.value
        else:
            self._completed_units.append(unit_id)
        tracker.complete_logical_unit(
            reused=was_cached or tracker.external_alfred_http_requests == before
        )
        self._checkpoint(None)

    def _semantic_information_set(
        self, indicator_id: str, as_of: date
    ) -> SemanticInformationSet | None:
        rows = list(
            self.session.scalars(
                select(ObservationVintage)
                .where(
                    ObservationVintage.indicator_id == indicator_id,
                    ObservationVintage.source == "fred",
                    ObservationVintage.vintage_date == as_of,
                    ObservationVintage.release_stage == "as_known_on",
                )
                .order_by(
                    ObservationVintage.observation_date,
                    ObservationVintage.revision_number,
                )
            ).all()
        )
        if not rows:
            return None
        payload = [
            {
                "indicator_id": item.indicator_id,
                "observation_date": item.observation_date,
                "value": item.current_value,
                "release_date": item.release_date,
                "vintage_date": item.vintage_date,
                "release_stage": item.release_stage,
                "source": item.source,
                "source_vintage_date": item.source_vintage_date,
            }
            for item in rows
        ]
        return SemanticInformationSet(
            indicator_id=indicator_id,
            as_of=as_of,
            observation_count=len(rows),
            latest_observation_date=max(item.observation_date for item in rows),
            semantic_hash=semantic_hash(payload),
        )

    def _events(
        self,
        selected: Sequence[ReleaseCandidate],
        *,
        baseline_date: date,
    ) -> list[ReleaseEvent]:
        grouped = group_candidates(selected)
        prior_hashes: dict[str, SemanticInformationSet | None] = {
            indicator_id: self._semantic_information_set(indicator_id, baseline_date)
            for indicator_id in {item.indicator_id for item in selected}
        }
        events: list[ReleaseEvent] = []
        for event_date, candidates in grouped.items():
            current = {
                item.indicator_id: snapshot
                for item in candidates
                if (snapshot := self._semantic_information_set(item.indicator_id, event_date))
                is not None
            }
            event = build_release_event(event_date, candidates, prior_hashes, current)
            if event is not None:
                events.append(event)
                for indicator_id in event.changed_indicator_ids:
                    prior_hashes[indicator_id] = current[indicator_id]
        return events

    def _production_engines(
        self, client: FredClient
    ) -> tuple[
        FeatureEngine,
        SignalEngine,
        PillarEngine,
        MacroStateEngine,
        MacroRegimeEngine,
    ]:
        async def resolver(*args: Any, **kwargs: Any) -> Any:
            return await get_effective_observations_on_demand(
                *args,
                **kwargs,
                settings=self.settings,
                client=client,
            )

        feature = FeatureEngine(self.session, resolver=resolver)
        signal = SignalEngine(
            self.session,
            source_registry=feature.source_registry,
            feature_catalog=feature.feature_catalog,
            feature_engine=feature,
        )
        pillar = PillarEngine(
            self.session,
            source_registry=feature.source_registry,
            signal_catalog=signal.signal_catalog,
            signal_engine=signal,
        )
        macro = MacroStateEngine(
            self.session,
            pillar_catalog=pillar.pillar_catalog,
            pillar_engine=pillar,
        )
        regime = MacroRegimeEngine(
            self.session,
            composite_catalog=macro.composite_catalog,
            macro_state_engine=macro,
        )
        return feature, signal, pillar, macro, regime

    async def _evaluate_date(
        self,
        *,
        evaluation_as_of: date,
        cadence: ValidationCadence,
        engines: tuple[
            FeatureEngine,
            SignalEngine,
            PillarEngine,
            MacroStateEngine,
            MacroRegimeEngine,
        ],
    ) -> tuple[CoverageSnapshot, RegimeResult, list[RegimeHistoryRow], list[QualityEvidence]]:
        feature_engine, signal_engine, pillar_engine, macro_engine, regime_engine = engines
        feature_ids = [
            feature.feature_id
            for indicator in feature_engine.feature_catalog.indicators
            for feature in indicator.features
            if feature.role == "primary"
        ]
        primary_signal_ids = [
            item.signal_id
            for item in signal_engine.signal_catalog.signals
            if item.feature_role == "primary"
        ]
        search_start = evaluation_as_of - relativedelta(
            months=pillar_engine.pillar_catalog.latest_known_search_months
        )
        feature_rows = await feature_engine.compute_feature_set(
            feature_ids, search_start, evaluation_as_of, evaluation_as_of
        )
        signal_rows = await signal_engine.compute_signal_set(
            primary_signal_ids, search_start, evaluation_as_of, evaluation_as_of
        )
        latest_features = _latest_by_id(feature_rows, id_field="feature_id")
        latest_signals = _latest_by_id(signal_rows, id_field="signal_id")
        pillars = await pillar_engine.compute_all_pillars(evaluation_as_of)
        state = await macro_engine.compute_macro_state(evaluation_as_of)
        regime = await regime_engine.compute_regime(evaluation_as_of)
        raw_sets = [
            self._semantic_information_set(policy.indicator_id, evaluation_as_of)
            for policy in self.catalog_source_policies()
        ]
        feature_count = sum(
            isinstance(item, FeatureResult)
            and item.feature_status is FeatureStatus.OK
            and item.value is not None
            for item in latest_features.values()
        )
        signal_count = sum(
            isinstance(item, NormalizedSignalResult)
            and item.signal_status is SignalStatus.OK
            and item.directed_value is not None
            for item in latest_signals.values()
        )
        pillar_count = sum(
            item.pillar_status is PillarStatus.OK and item.value is not None for item in pillars
        )
        macro_available = (
            state.historical_coverage_state is HistoricalCoverageState.FULL
            and state.macro_state_status is MacroStateStatus.OK
        )
        level_available = (
            macro_available
            and regime.base_regime_id is not None
            and regime.regime_status is RegimeStatus.OK
        )
        trajectory_available = (
            level_available
            and regime.growth_trajectory.status is TrajectoryStatus.OK
            and regime.inflation_trajectory.status is TrajectoryStatus.OK
        )
        hashes = [
            *(item.calculation_hash for item in latest_features.values()),
            *(item.calculation_hash for item in latest_signals.values()),
            *(item.calculation_hash for item in pillars),
            state.calculation_hash,
            regime.calculation_hash,
        ]
        snapshot = CoverageSnapshot(
            evaluation_as_of=evaluation_as_of,
            raw_available_count=sum(item is not None for item in raw_sets),
            feature_available_count=feature_count,
            signal_available_count=signal_count,
            pillar_available_count=pillar_count,
            macro_state_available=macro_available,
            regime_level_available=level_available,
            regime_trajectory_available=trajectory_available,
            calculation_hashes=hashes,
            source_evidence=[item.semantic_hash for item in raw_sets if item is not None],
        )
        fixed_status = "FULL" if signal_count == 33 else f"PARTIAL_{signal_count}_OF_33"
        dynamic_status = f"DYNAMIC_{signal_count}_OF_33"
        history = [
            _history_row(
                regime,
                universe_mode=UniverseMode.FULL_FIXED_UNIVERSE,
                cadence=cadence,
                validation_status=fixed_status,
            ),
            _history_row(
                regime,
                universe_mode=UniverseMode.DYNAMIC_REAL_TIME_UNIVERSE,
                cadence=cadence,
                validation_status=dynamic_status,
            ),
        ]
        semantic_dates: list[date | None] = []
        for feature_item in latest_features.values():
            assert isinstance(feature_item, FeatureResult)
            semantic_dates.extend(feature_item.input_observation_dates)
            semantic_dates.extend(feature_item.input_release_dates)
        for signal_item in latest_signals.values():
            semantic_dates.append(signal_item.feature_observation_date)
        for pillar_item in pillars:
            semantic_dates.extend(pillar_item.constituent_observation_dates.values())
        evidence = regime_quality_evidence(regime)
        leaked = future_dates(evaluation_as_of, semantic_dates)
        if leaked:
            evidence.append(
                QualityEvidence(
                    gate="FUTURE_LEAKAGE",
                    evaluation_as_of=evaluation_as_of,
                    detail=f"future semantic date {leaked[0].isoformat()}",
                    evidence_hash=semantic_hash(
                        {
                            "gate": "FUTURE_LEAKAGE",
                            "as_of": evaluation_as_of,
                            "first": leaked[0],
                        }
                    ),
                )
            )
        return snapshot, regime, history, evidence

    def _write_history_artifacts(
        self,
        histories: dict[tuple[UniverseMode, ValidationCadence], list[RegimeHistoryRow]],
    ) -> tuple[dict[str, str], dict[str, Any]]:
        run_hashes: dict[str, str] = {}
        metrics: dict[str, Any] = {}
        for (universe, cadence), rows in histories.items():
            namespace = f"{universe.value.lower()}__{cadence.value.lower()}"
            path, _artifact_hash = self.writer.write_csv(
                "production",
                f"{namespace}/regime_history.csv",
                [item.model_dump(mode="json") for item in rows],
            )
            self._artifact_paths.append(path)
            schedule = EvaluationSchedule(
                cadence=cadence,
                dates=[item.evaluation_as_of for item in rows],
                schedule_hash=semantic_hash(
                    {
                        "cadence": cadence,
                        "dates": [item.evaluation_as_of for item in rows],
                    }
                ),
            )
            run_hash = history_run_hash(
                validation_spec_hash=self.catalog.specification_hash,
                authorities=self.catalog.authorities,
                universe_mode=universe,
                schedule_hash=schedule.schedule_hash,
                rows=rows,
            )
            run_hashes[namespace] = run_hash
            metrics[namespace] = {
                "evaluation_count": len(rows),
                "transition_counts": transition_counts(rows),
                "event_count_occupancy": event_count_occupancy(rows),
                "calendar_duration_occupancy": calendar_duration_occupancy(rows),
                "durations": regime_durations(rows),
                "history_run_hash": run_hash,
            }
        return run_hashes, metrics

    def _hard_failure(self, evidence: Sequence[QualityEvidence]) -> bool:
        hard = set(self.catalog.quality_hard_gates)
        return any(item.gate in hard for item in evidence)

    async def run_pilot(self) -> PilotSummary:
        """Execute the approved bounded pilot; never start a full historical run."""
        if self.catalog.validation_phase is not ValidationPhase.PILOT:
            raise HistoricalValidationError("FULL_HISTORICAL_RUN_NOT_APPROVED")
        readiness = validate_data_layer(self.session)
        if not readiness.current_data_ready or not readiness.pit_backtest_ready:
            raise HistoricalValidationError("DATA_LAYER_NOT_READY")
        before_storage = storage_snapshot(
            {
                "raw": self.settings.raw_data_dir,
                "cache": self.settings.cache_dir,
                "stage3f": self.catalog.artifact_root,
            }
        )
        self._tracker = BudgetTracker(
            self.catalog.budgets.pilot,
            checkpoint=lambda reason: self._checkpoint(reason),
        )
        indicators = self._indicators()
        self._series_to_indicator = {
            item.fred_series_id: item.indicator_id
            for item in indicators
            if item.fred_series_id is not None
        }
        histories: dict[tuple[UniverseMode, ValidationCadence], list[RegimeHistoryRow]] = (
            defaultdict(list)
        )
        coverage: list[CoverageSnapshot] = []
        quality_evidence: list[QualityEvidence] = []
        events: list[ReleaseEvent] = []
        plan: MaterializationPlan | None = None
        selected: list[ReleaseCandidate] = []
        all_candidate_dates: dict[str, list[date]] = {}
        try:
            async with create_fred_client(
                self.settings, attempt_guard=self._attempt_guard
            ) as client:
                candidates, all_candidate_dates = await self._discover_candidates(
                    client, indicators
                )
                start, end, selected = propose_pilot_window(candidates, self.catalog.pilot_planner)
                selected_dates = sorted({item.event_date for item in selected})
                prior_dates = sorted(
                    {item.event_date for item in candidates if item.event_date < selected_dates[0]}
                )
                baseline_date = (
                    prior_dates[-1] if prior_dates else selected_dates[0] - timedelta(days=1)
                )
                provisional_release = EvaluationSchedule(
                    cadence=ValidationCadence.RELEASE_EVENT_EOD,
                    dates=selected_dates,
                    schedule_hash=semantic_hash(
                        {
                            "cadence": ValidationCadence.RELEASE_EVENT_EOD,
                            "dates": selected_dates,
                        }
                    ),
                )
                provisional = build_pilot_plan(
                    release_schedule=provisional_release,
                    window_start=start,
                    window_end=end,
                    indicator_ids=[item.indicator_id for item in indicators],
                    trajectory_horizon_months=(self.catalog.trajectory.production_horizon_months),
                    extra_snapshot_dates=[baseline_date],
                )
                self._schedule_hash = provisional.plan_hash
                discovery_units = [
                    item for item in provisional.units if item.unit_type == "VINTAGE_DISCOVERY"
                ]
                self._completed_units.extend(item.unit_id for item in discovery_units)
                for unit in provisional.units:
                    if unit.unit_type != "EXACT_PIT_SNAPSHOT":
                        continue
                    assert unit.indicator_id is not None and unit.as_of is not None
                    await self._materialize_snapshot(
                        client,
                        indicator_id=unit.indicator_id,
                        as_of=unit.as_of,
                        unit_id=unit.unit_id,
                    )
                events = self._events(selected, baseline_date=baseline_date)
                canonical_release = release_schedule(events)
                plan = build_pilot_plan(
                    release_schedule=canonical_release,
                    window_start=start,
                    window_end=end,
                    indicator_ids=[item.indicator_id for item in indicators],
                    trajectory_horizon_months=(self.catalog.trajectory.production_horizon_months),
                    extra_snapshot_dates=[baseline_date, *selected_dates],
                )
                self._schedule_hash = combined_schedule_hash(
                    plan.release_schedule, plan.month_end_schedule
                )
                engines = self._production_engines(client)
                for cadence, dates in (
                    (ValidationCadence.RELEASE_EVENT_EOD, plan.release_schedule.dates),
                    (ValidationCadence.MONTH_END_EOD, plan.month_end_schedule.dates),
                ):
                    for evaluation_as_of in dates:
                        unit = next(
                            item
                            for item in plan.units
                            if item.unit_type == "PRODUCTION_REPLAY"
                            and item.as_of == evaluation_as_of
                            and item.cadence == cadence
                        )
                        tracker = self._require_tracker()
                        tracker.begin_logical_unit()
                        snapshot, _regime, rows, evidence = await self._evaluate_date(
                            evaluation_as_of=evaluation_as_of,
                            cadence=cadence,
                            engines=engines,
                        )
                        coverage.append(snapshot)
                        for row in rows:
                            histories[(row.universe_mode, cadence)].append(row)
                        quality_evidence.extend(evidence)
                        self._completed_units.append(unit.unit_id)
                        tracker.complete_logical_unit(reused=True)
                        if self._hard_failure(evidence):
                            self._checkpoint(StopReason.HARD_QUALITY_FAILURE)
                            raise PilotQualityFailure(evidence[0].gate)
                        self._checkpoint(None)
        except BudgetStop:
            if plan is None:
                raise
        if plan is None:
            raise HistoricalValidationError("PILOT_PLAN_NOT_CREATED")

        boundaries, unresolved = discover_boundaries(
            coverage,
            authority_hashes=self.catalog.authorities.ordered_hashes(),
        )
        run_hashes, metrics = self._write_history_artifacts(histories)
        deterministic = all(
            value
            == history_run_hash(
                validation_spec_hash=self.catalog.specification_hash,
                authorities=self.catalog.authorities,
                universe_mode=universe,
                schedule_hash=semantic_hash(
                    {
                        "cadence": cadence,
                        "dates": [item.evaluation_as_of for item in rows],
                    }
                ),
                rows=list(rows),
            )
            for (universe, cadence), rows in histories.items()
            for value in [run_hashes[f"{universe.value.lower()}__{cadence.value.lower()}"]]
        )
        if not deterministic:
            quality_evidence.append(
                QualityEvidence(
                    gate="DETERMINISTIC_REPLAY_MISMATCH",
                    evaluation_as_of=None,
                    detail="semantic replay hash changed",
                    evidence_hash=semantic_hash("DETERMINISTIC_REPLAY_MISMATCH"),
                )
            )
        after_storage = storage_snapshot(
            {
                "raw": self.settings.raw_data_dir,
                "cache": self.settings.cache_dir,
                "stage3f": self.catalog.artifact_root,
            }
        )
        incremental = sum(after_storage.values()) - sum(before_storage.values())
        self._require_tracker().update_storage(incremental)
        grouped = group_candidates(selected)
        coalesced = sum(max(0, len(items) - 1) for items in grouped.values())
        future_count = sum(item.gate == "FUTURE_LEAKAGE" for item in quality_evidence)
        same_as_of_count = sum(
            item.gate == "CURRENT_LAG_SAME_AS_OF_VIOLATION" for item in quality_evidence
        )
        pit_unsafe_count = sum(
            item.gate == "CLASSIFIED_RESULT_PIT_UNSAFE" for item in quality_evidence
        )
        stop_reason = self._stop_reason or StopReason.PLANNER_COMPLETE
        status = (
            ValidationStatus.PILOT_COMPLETE
            if stop_reason is StopReason.PLANNER_COMPLETE
            and not quality_evidence
            and not self._failed_units
            else ValidationStatus.PARTIAL_RUN
        )
        artifact_map = artifact_hashes(self._artifact_paths)
        summary = PilotSummary(
            validation_phase=ValidationPhase.PILOT,
            validation_status=status,
            stop_reason=stop_reason,
            pilot_window_start=plan.window_start,
            pilot_window_end=plan.window_end,
            release_event_evaluations=len(plan.release_schedule.dates),
            month_end_evaluations=len(plan.month_end_schedule.dates),
            same_day_releases_coalesced=coalesced,
            counters=self._require_tracker().snapshot(),
            failed_logical_units=len(self._failed_units),
            failed_unit_status_counts=dict(Counter(self._failed_units.values())),
            future_leakage_count=future_count,
            same_as_of_violation_count=same_as_of_count,
            pit_unsafe_classified_count=pit_unsafe_count,
            deterministic_replay_match=deterministic,
            authority_drift_count=0,
            history_run_hashes=run_hashes,
            boundaries=boundaries,
            unresolved_boundaries=unresolved,
            artifact_hashes=artifact_map,
        )
        total_full_dates = len(
            {
                item
                for dates in all_candidate_dates.values()
                for item in dates
                if item >= date(2014, 5, 15)
            }
        )
        estimates = estimate_full_run(
            pilot_units=summary.counters.logical_pit_units,
            pilot_requests=summary.counters.external_alfred_http_requests,
            pilot_storage_bytes=incremental,
            pilot_runtime_seconds=str(summary.counters.elapsed_wall_seconds),
            pilot_evaluations=max(
                1, summary.release_event_evaluations + summary.month_end_evaluations
            ),
            full_evaluations=max(1, total_full_dates),
        )
        units_by_id = {item.unit_id: item for item in plan.units}
        manifest = {
            "validation_spec_version": self.catalog.validation_spec_version,
            "validation_spec_hash": self.catalog.specification_hash,
            "authorities": self.catalog.authorities.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
            "summary": summary.model_dump(mode="json"),
            "requests_by_indicator": dict(sorted(self._requests_by_indicator.items())),
            "requests_by_source_domain": {"ALFRED": summary.counters.external_alfred_http_requests},
            "requests_by_endpoint": dict(sorted(self._requests_by_endpoint.items())),
            "storage_before": before_storage,
            "storage_after": after_storage,
            "incremental_storage_bytes": incremental,
            "metrics": metrics,
            "quality_evidence": [item.model_dump(mode="json") for item in quality_evidence],
            "failed_units": [
                {
                    "unit_id": unit_id,
                    "status": status,
                    "unit_type": units_by_id[unit_id].unit_type,
                    "indicator_id": units_by_id[unit_id].indicator_id,
                    "evaluation_as_of": units_by_id[unit_id].as_of,
                }
                for unit_id, status in sorted(self._failed_units.items())
            ],
            "full_run_estimate": estimates,
            "full_run_approved": False,
        }
        manifest_path, _ = self.writer.write_json("production", "manifest.json", manifest)
        status_path, _ = self.writer.write_json("production", "status.json", summary)
        coverage_path, _ = self.writer.write_csv(
            "production",
            "coverage_inventory.csv",
            [item.model_dump(mode="json") for item in coverage],
        )
        events_path, _ = self.writer.write_csv(
            "production",
            "release_event_schedule.csv",
            [item.model_dump(mode="json") for item in events],
        )
        month_path, _ = self.writer.write_csv(
            "production",
            "month_end_schedule.csv",
            [{"evaluation_as_of": item} for item in plan.month_end_schedule.dates],
        )
        self._artifact_paths.extend(
            [manifest_path, status_path, coverage_path, events_path, month_path]
        )
        secret = (
            self.settings.fred_api_key.get_secret_value()
            if self.settings.fred_api_key is not None
            else None
        )
        for path in self._artifact_paths:
            if path.is_file() and contains_credential(
                path.read_text(encoding="utf-8", errors="ignore"), secret
            ):
                self._checkpoint(StopReason.HARD_QUALITY_FAILURE)
                raise PilotQualityFailure("CREDENTIAL_LEAKAGE")
        report_lines = [
            "# Stage 3F bounded pilot",
            "",
            f"- Validation: `{self.catalog.validation_spec_version}` / `{self.catalog.specification_hash}`",
            f"- Window: {plan.window_start} through {plan.window_end}",
            f"- Status: {summary.validation_status.value}",
            f"- Release-event evaluations: {summary.release_event_evaluations}",
            f"- Month-end evaluations: {summary.month_end_evaluations}",
            f"- Logical PIT units: {summary.counters.logical_pit_units}",
            f"- External ALFRED HTTP attempts: {summary.counters.external_alfred_http_requests}",
            f"- Retries: {summary.counters.retry_count}",
            f"- Cache/local reuse: {summary.counters.cache_reuse_count}",
            f"- Failed logical units: {summary.failed_logical_units}",
            f"- Failed unit statuses: {summary.failed_unit_status_counts}",
            f"- Future leakage: {summary.future_leakage_count}",
            f"- Same-as-of violations: {summary.same_as_of_violation_count}",
            f"- PIT-unsafe classified results: {summary.pit_unsafe_classified_count}",
            f"- Deterministic replay: {summary.deterministic_replay_match}",
            "- Full historical materialization: NOT AUTHORIZED",
        ]
        report_path, _ = self.writer.write_markdown("stage3f_pilot_report.md", report_lines)
        self._artifact_paths.append(report_path)
        self._checkpoint(stop_reason)
        return summary
