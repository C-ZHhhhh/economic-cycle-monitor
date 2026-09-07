"""Stage 3E base classification, trajectory, PIT, provenance, and cache tests."""

import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from app.composites.config import load_composite_catalog
from app.composites.schemas import (
    EvaluationMode,
    GrowthComponentResult,
    GrowthStatus,
    HistoricalCoverageState,
    MacroStateQualityFlag,
    MacroStateResult,
    MacroStateStatus,
    PillarComponentSnapshot,
)
from app.pillars.schemas import PillarStatus
from app.regimes.cache import RegimeResultCache
from app.regimes.config import load_regime_catalog
from app.regimes.schemas import (
    GrowthCondition,
    GrowthTrajectoryQualifier,
    InflationCondition,
    InflationTrajectoryQualifier,
    RegimeQualityFlag,
    RegimeResult,
    RegimeStatus,
    TrajectoryStatus,
)
from app.regimes.service import MacroRegimeEngine, compute_macro_regime
from app.source_registry.config import load_source_registry
from app.source_registry.materialization import load_pit_materialization_catalog
from app.source_registry.revision_provenance import (
    build_layer_effect,
    load_revision_provenance_catalog,
)
from app.source_registry.revision_schemas import RevisionEffectType

COMPOSITE = load_composite_catalog()
REGIMES = load_regime_catalog(composite_catalog=COMPOSITE)
AS_OF = date(2022, 7, 31)
LAG = date(2022, 4, 30)
GROWTH_IDS = [item.pillar_id for item in COMPOSITE.growth.pillars]
INFLATION_ID = COMPOSITE.inflation_pillar_id
CONTEXT_IDS = COMPOSITE.context_pillar_ids
TWO_THIRDS = REGIMES.growth.minimum_confidence.value
REGISTRY = load_source_registry()
MATERIALIZATION = load_pit_materialization_catalog(source_registry=REGISTRY)
REVISION_PROVENANCE = load_revision_provenance_catalog(
    source_registry=REGISTRY,
    materialization=MATERIALIZATION,
)
_UNSET = object()


class FakeMacroStateEngine:
    """In-memory Stage 3D boundary with no lower-layer access."""

    def __init__(self, states: dict[date | None, MacroStateResult]) -> None:
        self.states = states
        self.calls: list[date | None] = []

    async def compute_macro_state(self, evaluation_as_of: date | None = None) -> MacroStateResult:
        self.calls.append(evaluation_as_of)
        return self.states[evaluation_as_of].model_copy(deep=True)


def _growth_result(
    value: Decimal | None,
    confidence: Decimal,
    status: GrowthStatus,
) -> GrowthComponentResult:
    available = value is not None
    values = {pillar_id: value if available else None for pillar_id in GROWTH_IDS}
    return GrowthComponentResult(
        component_id=COMPOSITE.growth.component_id,
        value=value,
        unit=COMPOSITE.growth.unit,
        semantic_direction=COMPOSITE.growth.semantic_direction,
        status=status,
        confidence=confidence,
        coverage=Decimal(1) if available else Decimal(0),
        expected_pillar_count=4,
        valid_pillar_count=4 if available else 0,
        aggregation_method=COMPOSITE.growth.aggregation_method,
        pillar_ids=GROWTH_IDS,
        pillar_values=values,
        pillar_confidences={pillar_id: confidence for pillar_id in GROWTH_IDS},
        pillar_statuses={
            pillar_id: PillarStatus.OK if available else PillarStatus.NO_VALID_SIGNALS
            for pillar_id in GROWTH_IDS
        },
        pillar_base_weights={pillar_id: Decimal("0.25") for pillar_id in GROWTH_IDS},
        pillar_effective_weights={
            pillar_id: Decimal("0.25") if available else Decimal(0) for pillar_id in GROWTH_IDS
        },
        pillar_calculation_hashes={pillar_id: f"pillar:{pillar_id}" for pillar_id in GROWTH_IDS},
        positive_pillar_count=4 if value is not None and value > 0 else 0,
        negative_pillar_count=4 if value is not None and value < 0 else 0,
        neutral_pillar_count=4 if value == 0 else 0,
        breadth=(Decimal(1) if value is not None and value > 0 else Decimal(-1))
        if value not in {None, Decimal(0)}
        else (Decimal(0) if value == 0 else None),
        dispersion=Decimal(0) if available else None,
    )


def _snapshot(
    pillar_id: str,
    value: Decimal | None,
    confidence: Decimal,
) -> PillarComponentSnapshot:
    return PillarComponentSnapshot(
        pillar_id=pillar_id,
        value=value,
        confidence=confidence,
        status=PillarStatus.OK if value is not None else PillarStatus.NO_VALID_SIGNALS,
        semantic_direction="typed upstream semantics",
        calculation_hash=f"snapshot:{pillar_id}:{value}",
    )


def _state(
    as_of: date,
    *,
    growth: Decimal | None = Decimal("-0.084"),
    inflation: Decimal | None = Decimal("0.799"),
    growth_confidence: Decimal = Decimal(1),
    inflation_confidence: Decimal = Decimal(1),
    macro_status: MacroStateStatus = MacroStateStatus.OK,
    mode: EvaluationMode = EvaluationMode.PIT_DATE_EOD,
    pit_safe: bool | None = None,
    flags: list[MacroStateQualityFlag] | None = None,
    context_values: tuple[Decimal | None, ...] = (
        Decimal("0.1"),
        Decimal("0.2"),
        Decimal("0.3"),
        Decimal("0.4"),
    ),
    suffix: str = "base",
    counterfactual_growth: Decimal | None | object = _UNSET,
) -> MacroStateResult:
    resolved_pit_safe = mode is EvaluationMode.PIT_DATE_EOD if pit_safe is None else pit_safe
    growth_status = (
        GrowthStatus.PARTIAL
        if macro_status is MacroStateStatus.PARTIAL and growth is not None
        else GrowthStatus.OK
        if growth is not None
        else GrowthStatus.INSUFFICIENT_GROWTH_COVERAGE
    )
    revision_effect = None
    if counterfactual_growth is not _UNSET:
        revision_effect = build_layer_effect(
            authority=REVISION_PROVENANCE,
            source_evidence_hashes=("e" * 64,),
            source_effect_types=(RevisionEffectType.SOURCE_REVISION,),
            current_value=growth,
            counterfactual_value=counterfactual_growth,  # type: ignore[arg-type]
            current_status=macro_status.value,
            counterfactual_status=macro_status.value,
        )
    resolved_flags = list(flags or [])
    if revision_effect is not None and revision_effect.result_changed:
        resolved_flags.append(MacroStateQualityFlag.REVISION_CHANGED_RESULT)
    return MacroStateResult(
        state_id=COMPOSITE.state_id,
        composite_spec_version=COMPOSITE.composite_spec_version,
        composite_spec_hash=COMPOSITE.specification_hash,
        evaluation_mode=mode,
        evaluation_as_of=as_of,
        growth=_growth_result(growth, growth_confidence, growth_status),
        inflation=_snapshot(INFLATION_ID, inflation, inflation_confidence),
        context=[
            _snapshot(pillar_id, value, Decimal(1) if value is not None else Decimal(0))
            for pillar_id, value in zip(CONTEXT_IDS, context_values, strict=True)
        ],
        historical_coverage_state=(
            HistoricalCoverageState.FULL
            if macro_status is MacroStateStatus.OK
            else HistoricalCoverageState.PARTIAL
        ),
        source_registry_hash=COMPOSITE.required_source_registry_hash,
        feature_spec_version=COMPOSITE.required_feature_spec_version,
        normalization_spec_version=COMPOSITE.required_normalization_spec_version,
        normalization_spec_hash=COMPOSITE.required_normalization_spec_hash,
        pillar_spec_version=COMPOSITE.required_pillar_spec_version,
        pillar_spec_hash=COMPOSITE.required_pillar_spec_hash,
        is_point_in_time_safe=resolved_pit_safe,
        macro_state_status=macro_status,
        quality_flags=resolved_flags,
        calculation_hash=f"state:{as_of}:{growth}:{inflation}:{suffix}",
        revision_provenance_version=(REVISION_PROVENANCE.revision_provenance_version),
        revision_provenance_hash=REVISION_PROVENANCE.specification_hash,
        revision_evidence_hashes=(
            revision_effect.source_revision_evidence_hashes if revision_effect is not None else ()
        ),
        revision_effect=revision_effect,
    )


def _engine(
    current: MacroStateResult,
    lagged: MacroStateResult,
    *,
    current_key: date | None = AS_OF,
    cache: RegimeResultCache | None = None,
) -> tuple[MacroRegimeEngine, FakeMacroStateEngine]:
    fake = FakeMacroStateEngine({current_key: current, lagged.evaluation_as_of: lagged})
    engine = MacroRegimeEngine(
        Session(),
        composite_catalog=COMPOSITE,
        regime_catalog=REGIMES,
        macro_state_engine=fake,
        cache=cache if cache is not None else RegimeResultCache(),
    )
    return engine, fake


@pytest.mark.parametrize(
    ("growth", "inflation", "growth_condition", "inflation_condition", "regime_id"),
    [
        (growth_value, inflation_value, growth_condition, inflation_condition, definition.regime_id)
        for growth_value, growth_condition in [
            (Decimal("-0.2"), GrowthCondition.WEAK),
            (Decimal("0"), GrowthCondition.NEUTRAL),
            (Decimal("0.2"), GrowthCondition.STRONG),
        ]
        for inflation_value, inflation_condition in [
            (Decimal("-0.2"), InflationCondition.LOW_PRESSURE),
            (Decimal("0"), InflationCondition.NEUTRAL_PRESSURE),
            (Decimal("0.2"), InflationCondition.HIGH_PRESSURE),
        ]
        for definition in [REGIMES.by_conditions()[(growth_condition, inflation_condition)]]
    ],
)
@pytest.mark.asyncio
async def test_exact_nine_base_regimes(
    growth: Decimal,
    inflation: Decimal,
    growth_condition: GrowthCondition,
    inflation_condition: InflationCondition,
    regime_id: str,
) -> None:
    current = _state(AS_OF, growth=growth, inflation=inflation)
    lagged = _state(LAG, growth=growth, inflation=inflation)
    result = await _engine(current, lagged)[0].compute_regime(AS_OF)
    assert result.growth_condition is growth_condition
    assert result.inflation_condition is inflation_condition
    assert result.base_regime_id == regime_id
    assert result.regime_status is RegimeStatus.OK


@pytest.mark.asyncio
async def test_exact_two_thirds_confidence_is_accepted() -> None:
    current = _state(AS_OF, growth_confidence=TWO_THIRDS, inflation_confidence=TWO_THIRDS)
    lagged = _state(LAG, growth_confidence=TWO_THIRDS, inflation_confidence=TWO_THIRDS)
    result = await _engine(current, lagged)[0].compute_regime(AS_OF)
    assert result.base_regime_id is not None
    assert result.growth_trajectory.status is TrajectoryStatus.OK
    assert result.inflation_trajectory.status is TrajectoryStatus.OK


@pytest.mark.asyncio
async def test_just_below_confidence_rejects_base_but_preserves_conditions() -> None:
    below = Decimal("0.6666666666666666666666666666")
    current = _state(AS_OF, growth_confidence=below)
    result = await _engine(current, _state(LAG))[0].compute_regime(AS_OF)
    assert result.regime_status is RegimeStatus.INSUFFICIENT_STATE
    assert result.base_regime_id is None
    assert result.growth_condition is GrowthCondition.NEUTRAL
    assert result.inflation_condition is InflationCondition.HIGH_PRESSURE
    assert RegimeQualityFlag.LOW_DATA_CONFIDENCE in result.quality_flags


@pytest.mark.asyncio
async def test_partial_current_state_is_classified_without_status_upgrade() -> None:
    today = date.today()
    lag = date(today.year, today.month, today.day) - relativedelta(months=3)
    current = _state(
        today,
        growth=Decimal("-0.03112"),
        inflation=Decimal("-0.01166"),
        growth_confidence=Decimal("0.6875"),
        inflation_confidence=TWO_THIRDS,
        macro_status=MacroStateStatus.PARTIAL,
        mode=EvaluationMode.CURRENT,
        pit_safe=False,
    )
    lagged = _state(lag, growth=Decimal("-0.2"), inflation=Decimal("-0.2"))
    result = await _engine(current, lagged, current_key=None)[0].compute_regime()
    assert result.growth_condition is GrowthCondition.NEUTRAL
    assert result.inflation_condition is InflationCondition.NEUTRAL_PRESSURE
    assert result.base_regime_id == ("US_MACRO_REGIME__GROWTH_NEUTRAL__INFLATION_PRESSURE_NEUTRAL")
    assert result.regime_status is RegimeStatus.PARTIAL
    assert result.macro_state_status is MacroStateStatus.PARTIAL
    assert RegimeQualityFlag.LOW_DATA_CONFIDENCE in result.quality_flags
    assert not result.is_point_in_time_safe


@pytest.mark.asyncio
async def test_missing_growth_fails_closed() -> None:
    current = _state(AS_OF, growth=None, growth_confidence=Decimal(0))
    result = await _engine(current, _state(LAG))[0].compute_regime(AS_OF)
    assert result.regime_status is RegimeStatus.INSUFFICIENT_STATE
    assert result.growth_condition is None
    assert result.inflation_condition is InflationCondition.HIGH_PRESSURE
    assert result.base_regime_id is None


@pytest.mark.asyncio
async def test_missing_inflation_preserves_growth_condition_but_not_base() -> None:
    current = _state(AS_OF, inflation=None, inflation_confidence=Decimal(0))
    result = await _engine(current, _state(LAG))[0].compute_regime(AS_OF)
    assert result.regime_status is RegimeStatus.MISSING_INFLATION
    assert result.growth_condition is GrowthCondition.NEUTRAL
    assert result.inflation_condition is None
    assert result.base_regime_id is None


@pytest.mark.asyncio
async def test_context_is_passed_through_and_never_changes_classification() -> None:
    baseline = await _engine(_state(AS_OF), _state(LAG))[0].compute_regime(AS_OF)
    changed = await _engine(
        _state(
            AS_OF,
            context_values=(Decimal("-0.9"), None, Decimal("0.9"), Decimal("0")),
            suffix="context-changed",
        ),
        _state(LAG),
    )[0].compute_regime(AS_OF)
    assert changed.base_regime_id == baseline.base_regime_id
    assert changed.growth_condition == baseline.growth_condition
    assert changed.inflation_condition == baseline.inflation_condition
    assert [item.value for item in changed.context] == [Decimal("-0.9"), None, Decimal("0.9"), 0]


@pytest.mark.parametrize(
    ("current_growth", "lag_growth", "expected"),
    [
        (Decimal("0.2"), Decimal("0"), GrowthTrajectoryQualifier.IMPROVING),
        (Decimal("0.1"), Decimal("0"), GrowthTrajectoryQualifier.STABLE),
        (Decimal("-0.1"), Decimal("0"), GrowthTrajectoryQualifier.STABLE),
        (Decimal("-0.2"), Decimal("0"), GrowthTrajectoryQualifier.DETERIORATING),
    ],
)
@pytest.mark.asyncio
async def test_growth_real_time_path_trajectory(
    current_growth: Decimal,
    lag_growth: Decimal,
    expected: GrowthTrajectoryQualifier,
) -> None:
    result = await _engine(
        _state(AS_OF, growth=current_growth, suffix="current"),
        _state(LAG, growth=lag_growth, suffix="lag"),
    )[0].compute_regime(AS_OF)
    trajectory = result.growth_trajectory
    assert trajectory.qualifier is expected
    assert trajectory.delta == current_growth - lag_growth
    assert trajectory.current_state_hash.endswith(":current")
    assert trajectory.lag_state_hash is not None and trajectory.lag_state_hash.endswith(":lag")
    assert trajectory.current_as_of == AS_OF
    assert trajectory.lag_as_of == LAG


@pytest.mark.parametrize(
    ("current_inflation", "lag_inflation", "expected"),
    [
        (Decimal("0.2"), Decimal("0"), InflationTrajectoryQualifier.RISING),
        (Decimal("0.1"), Decimal("0"), InflationTrajectoryQualifier.STABLE),
        (Decimal("-0.1"), Decimal("0"), InflationTrajectoryQualifier.STABLE),
        (Decimal("-0.2"), Decimal("0"), InflationTrajectoryQualifier.FALLING),
    ],
)
@pytest.mark.asyncio
async def test_inflation_real_time_path_trajectory(
    current_inflation: Decimal,
    lag_inflation: Decimal,
    expected: InflationTrajectoryQualifier,
) -> None:
    result = await _engine(
        _state(AS_OF, inflation=current_inflation),
        _state(LAG, inflation=lag_inflation),
    )[0].compute_regime(AS_OF)
    assert result.inflation_trajectory.qualifier is expected
    assert result.inflation_trajectory.delta == current_inflation - lag_inflation


@pytest.mark.asyncio
async def test_insufficient_trajectory_history_does_not_invalidate_base() -> None:
    lagged = _state(LAG, growth=None, growth_confidence=Decimal(0))
    result = await _engine(_state(AS_OF), lagged)[0].compute_regime(AS_OF)
    assert result.base_regime_id is not None
    assert result.regime_status is RegimeStatus.OK
    assert result.growth_trajectory.status is TrajectoryStatus.INSUFFICIENT_HISTORY
    assert result.growth_trajectory.delta is None
    assert result.growth_trajectory.qualifier is None


@pytest.mark.asyncio
async def test_lag_confidence_gate_is_axis_specific() -> None:
    lagged = _state(LAG, growth_confidence=Decimal("0.6666666666666666666666666666"))
    result = await _engine(_state(AS_OF), lagged)[0].compute_regime(AS_OF)
    assert result.base_regime_id is not None
    assert result.growth_trajectory.status is TrajectoryStatus.LOW_DATA_CONFIDENCE
    assert result.inflation_trajectory.status is TrajectoryStatus.OK


@pytest.mark.asyncio
async def test_lag_pit_unsafe_is_independent_of_valid_base() -> None:
    lagged = _state(LAG).model_copy(
        update={
            "macro_state_status": MacroStateStatus.PIT_UNSAFE,
            "is_point_in_time_safe": False,
        }
    )
    result = await _engine(_state(AS_OF), lagged)[0].compute_regime(AS_OF)
    assert result.base_regime_id is not None
    assert result.regime_status is RegimeStatus.OK
    assert result.growth_trajectory.status is TrajectoryStatus.PIT_UNSAFE
    assert result.inflation_trajectory.status is TrajectoryStatus.PIT_UNSAFE
    assert not result.is_point_in_time_safe


@pytest.mark.asyncio
async def test_margin_exact_warning_boundary_is_inclusive() -> None:
    result = await _engine(
        _state(AS_OF, growth=Decimal("0.15"), inflation=Decimal("0")), _state(LAG)
    )[0].compute_regime(AS_OF)
    assert result.growth_classification_margin == Decimal("0.05")
    assert RegimeQualityFlag.NEAR_REGIME_BOUNDARY in result.quality_flags


@pytest.mark.asyncio
async def test_margin_just_above_warning_boundary_has_no_flag() -> None:
    result = await _engine(
        _state(AS_OF, growth=Decimal("0.1500001"), inflation=Decimal("0")), _state(LAG)
    )[0].compute_regime(AS_OF)
    assert result.growth_classification_margin == Decimal("0.0500001")
    assert RegimeQualityFlag.NEAR_REGIME_BOUNDARY not in result.quality_flags


@pytest.mark.asyncio
async def test_historical_and_revision_flags_propagate_without_transition_flag() -> None:
    current = _state(
        AS_OF,
        flags=[
            MacroStateQualityFlag.HISTORICAL_COVERAGE_LIMITATION,
        ],
        counterfactual_growth=Decimal("-0.2"),
    )
    result = await _engine(current, _state(LAG))[0].compute_regime(AS_OF)
    assert RegimeQualityFlag.HISTORICAL_COVERAGE_LIMITATION in result.quality_flags
    assert RegimeQualityFlag.REVISION_CHANGED_RESULT in result.quality_flags
    assert all(item.value != "REGIME_TRANSITION" for item in result.quality_flags)


@pytest.mark.asyncio
async def test_legacy_macro_revision_flag_without_evidence_does_not_propagate() -> None:
    current = _state(
        AS_OF,
        flags=[MacroStateQualityFlag.REVISION_CHANGED_RESULT],
    )
    result = await _engine(current, _state(LAG))[0].compute_regime(AS_OF)
    assert RegimeQualityFlag.REVISION_CHANGED_RESULT not in result.quality_flags
    assert result.revision_effect is None


@pytest.mark.asyncio
async def test_current_and_pit_use_the_same_classifier_path() -> None:
    today = date.today()
    current_lag = today - relativedelta(months=3)
    current_state = _state(
        today,
        growth=Decimal("0"),
        inflation=Decimal("0"),
        mode=EvaluationMode.CURRENT,
        pit_safe=False,
    )
    pit_state = _state(AS_OF, growth=Decimal("0"), inflation=Decimal("0"))
    current = await _engine(
        current_state,
        _state(current_lag, growth=Decimal("0"), inflation=Decimal("0")),
        current_key=None,
    )[0].compute_regime()
    pit = await _engine(pit_state, _state(LAG, growth=Decimal("0"), inflation=Decimal("0")))[
        0
    ].compute_regime(AS_OF)
    assert current.base_regime_id == pit.base_regime_id
    assert current.growth_condition == pit.growth_condition
    assert current.inflation_condition == pit.inflation_condition
    assert not current.is_point_in_time_safe
    assert pit.is_point_in_time_safe


@pytest.mark.asyncio
async def test_current_contract_mismatch_fails_closed() -> None:
    current = _state(AS_OF).model_copy(update={"composite_spec_hash": "wrong"})
    result = await _engine(current, _state(LAG))[0].compute_regime(AS_OF)
    assert result.regime_status is RegimeStatus.SPEC_MISMATCH
    assert result.base_regime_id is None
    assert result.growth_trajectory.status is TrajectoryStatus.SPEC_MISMATCH


@pytest.mark.asyncio
async def test_as_of_mismatch_fails_closed() -> None:
    current = _state(AS_OF).model_copy(update={"evaluation_as_of": date(2022, 7, 30)})
    result = await _engine(current, _state(LAG))[0].compute_regime(AS_OF)
    assert result.regime_status is RegimeStatus.AS_OF_MISMATCH
    assert result.base_regime_id is None
    assert result.growth_trajectory.status is TrajectoryStatus.PIT_UNSAFE


@pytest.mark.asyncio
async def test_current_pit_unsafe_fails_closed() -> None:
    current = _state(AS_OF).model_copy(
        update={
            "macro_state_status": MacroStateStatus.PIT_UNSAFE,
            "is_point_in_time_safe": False,
        }
    )
    result = await _engine(current, _state(LAG))[0].compute_regime(AS_OF)
    assert result.regime_status is RegimeStatus.PIT_UNSAFE
    assert result.base_regime_id is None


@pytest.mark.asyncio
async def test_revision_can_cross_boundary_and_changes_hash() -> None:
    first_as_of = date(2020, 4, 29)
    first_lag = date(2020, 1, 29)
    second_as_of = date(2020, 5, 28)
    second_lag = date(2020, 2, 28)
    first = await _engine(
        _state(first_as_of, growth=Decimal("-0.1001"), suffix="first"),
        _state(first_lag),
        current_key=first_as_of,
    )[0].compute_regime(first_as_of)
    second = await _engine(
        _state(
            second_as_of,
            growth=Decimal("-0.0999"),
            counterfactual_growth=Decimal("-0.1001"),
            suffix="second",
        ),
        _state(second_lag),
        current_key=second_as_of,
    )[0].compute_regime(second_as_of)
    assert first.growth_condition is GrowthCondition.WEAK
    assert second.growth_condition is GrowthCondition.NEUTRAL
    assert first.base_regime_id != second.base_regime_id
    assert first.calculation_hash != second.calculation_hash
    assert RegimeQualityFlag.REVISION_CHANGED_RESULT in second.quality_flags


@pytest.mark.asyncio
async def test_cache_is_deterministic_copy_safe_and_bounded() -> None:
    cache = RegimeResultCache(max_entries=1)
    engine, fake = _engine(_state(AS_OF), _state(LAG), cache=cache)
    first = await engine.compute_regime(AS_OF)
    second = await engine.compute_regime(AS_OF)
    assert first == second
    assert first is not second
    assert first.calculation_hash == second.calculation_hash
    assert len(cache) == 1
    assert fake.calls == [AS_OF, LAG, AS_OF, LAG]


@pytest.mark.asyncio
async def test_current_and_pit_cache_keys_are_isolated() -> None:
    today = date.today()
    current_lag = today - relativedelta(months=3)
    fake = FakeMacroStateEngine(
        {
            None: _state(today, mode=EvaluationMode.CURRENT, pit_safe=False, suffix="current"),
            current_lag: _state(current_lag, suffix="current-lag"),
            AS_OF: _state(AS_OF, suffix="pit"),
            LAG: _state(LAG, suffix="pit-lag"),
        }
    )
    cache = RegimeResultCache(max_entries=2)
    engine = MacroRegimeEngine(
        Session(),
        composite_catalog=COMPOSITE,
        regime_catalog=REGIMES,
        macro_state_engine=fake,
        cache=cache,
    )
    current = await engine.compute_regime()
    pit = await engine.compute_regime(AS_OF)
    assert current.evaluation_mode is EvaluationMode.CURRENT
    assert current.revision_provenance_version is None
    assert current.revision_provenance_hash is None
    assert current.revision_effect is None
    assert pit.evaluation_mode is EvaluationMode.PIT_DATE_EOD
    assert current.calculation_hash != pit.calculation_hash
    assert len(cache) == 2


def test_public_domain_api_accepts_no_provider_weights_formulas_or_previous_state() -> None:
    parameters = inspect.signature(compute_macro_regime).parameters
    assert set(parameters) == {"session", "evaluation_as_of"}


def test_result_has_no_merged_regime_confidence() -> None:
    assert "regime_confidence" not in RegimeResult.model_fields


def test_production_service_has_no_lower_layer_or_state_machine_authority() -> None:
    source = inspect.getsource(MacroRegimeEngine)
    assert "PillarResult" not in source
    assert "NormalizedSignalResult" not in source
    assert "FeatureResult" not in source
    assert "Observation" not in source
    assert "previous_regime" not in source
    assert "entered_at" not in source
    assert "regime_duration" not in source
    assert "derived_features" not in source


def test_production_regime_package_contains_no_prohibited_product_logic() -> None:
    package = Path("app/regimes")
    text = "\n".join(path.read_text(encoding="utf-8").lower() for path in package.glob("*.py"))
    forbidden = ["nber", "market optimization", "portfolio rule", "llm prompt"]
    assert not any(term in text for term in forbidden)
