"""Stage 3D three-layer service, PIT, status, provenance, and cache tests."""

import inspect
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.composites.cache import MacroStateResultCache
from app.composites.config import load_composite_catalog
from app.composites.schemas import (
    EvaluationMode,
    GrowthStatus,
    HistoricalCoverageState,
    MacroStateQualityFlag,
    MacroStateStatus,
)
from app.composites.service import MacroStateEngine, compute_macro_state
from app.pillars.config import load_pillar_catalog
from app.pillars.schemas import (
    AggregationMethod,
    ConcentrationMetadata,
    ConfidenceComponents,
    ContributionMapping,
    ContributionMappingParameters,
    PillarQualityFlag,
    PillarResult,
    PillarStatus,
)
from app.source_registry.config import load_source_registry
from app.source_registry.materialization import load_pit_materialization_catalog
from app.source_registry.revision_provenance import (
    build_layer_effect,
    load_revision_provenance_catalog,
)
from app.source_registry.revision_schemas import RevisionEffectType

PILLARS = load_pillar_catalog()
COMPOSITE = load_composite_catalog(pillar_catalog=PILLARS)
AS_OF = date(2020, 5, 31)
ACTIVITY = "US_MACRO__REAL_ACTIVITY_AND_DEMAND"
LABOR = "US_MACRO__LABOR_STRENGTH"
INFLATION = "US_MACRO__INFLATION_PRESSURE"
MANUFACTURING = "US_MACRO__MANUFACTURING_AND_INVESTMENT"
HOUSING = "US_MACRO__HOUSING_STRENGTH"
GROWTH_IDS = [item.pillar_id for item in COMPOSITE.growth.pillars]
CONTEXT_IDS = COMPOSITE.context_pillar_ids
REGISTRY = load_source_registry()
MATERIALIZATION = load_pit_materialization_catalog(source_registry=REGISTRY)
REVISION_PROVENANCE = load_revision_provenance_catalog(
    source_registry=REGISTRY,
    materialization=MATERIALIZATION,
)
_UNSET = object()


class FakePillarEngine:
    """In-memory Stage 3C boundary with no signal, feature, or provider access."""

    def __init__(self, rows: list[PillarResult]) -> None:
        self.rows = rows
        self.calls: list[date | None] = []

    async def compute_all_pillars(self, evaluation_as_of: date | None = None) -> list[PillarResult]:
        self.calls.append(evaluation_as_of)
        return [item.model_copy(deep=True) for item in self.rows]


def _valid_count(definition_count: int, confidence: Decimal) -> int:
    count = int(confidence * definition_count)
    if Decimal(count) / Decimal(definition_count) != confidence:
        raise ValueError("Fixture confidence must correspond to an exact constituent count")
    return count


def _pillar(
    pillar_id: str,
    value: Decimal | None,
    *,
    as_of: date = AS_OF,
    status: PillarStatus | None = None,
    confidence: Decimal | None = None,
    pit_safe: bool = True,
    flags: list[PillarQualityFlag] | None = None,
    revision: int = 0,
    counterfactual_value: Decimal | None | object = _UNSET,
) -> PillarResult:
    definition = PILLARS.by_pillar()[pillar_id]
    resolved_status = status or (
        PillarStatus.OK if value is not None else PillarStatus.NO_VALID_SIGNALS
    )
    resolved_confidence = (
        confidence if confidence is not None else (Decimal(1) if value is not None else Decimal(0))
    )
    expected = len(definition.primary_signal_ids)
    valid = _valid_count(expected, resolved_confidence)
    constituent_values = {
        signal_id: Decimal(0) if index < valid else None
        for index, signal_id in enumerate(definition.primary_signal_ids)
    }
    revision_effect = None
    if counterfactual_value is not _UNSET:
        revision_effect = build_layer_effect(
            authority=REVISION_PROVENANCE,
            source_evidence_hashes=("e" * 64,),
            source_effect_types=(RevisionEffectType.SOURCE_REVISION,),
            current_value=value,
            counterfactual_value=counterfactual_value,  # type: ignore[arg-type]
            current_status=resolved_status.value,
            counterfactual_status=resolved_status.value,
        )
    resolved_flags = list(flags or [])
    if revision_effect is not None and revision_effect.result_changed:
        resolved_flags.append(PillarQualityFlag.REVISION_CHANGED_RESULT)
    return PillarResult(
        pillar_id=pillar_id,
        display_name=definition.display_name,
        description=definition.description,
        pillar_role=definition.pillar_role,
        core_composite_eligible=definition.core_composite_eligible,
        pillar_spec_version=PILLARS.pillar_spec_version,
        pillar_spec_hash=PILLARS.specification_hash,
        evaluation_as_of=as_of,
        value=value,
        unit="bounded_symmetric_score",
        semantic_direction=definition.semantic_direction,
        confidence=resolved_confidence,
        confidence_components=ConfidenceComponents(
            coverage=resolved_confidence,
            source_health_coverage=Decimal(1),
            freshness_coverage=Decimal(1),
            agreement_ratio=None,
            revision_state="CHANGED" if revision else "UNCHANGED",
        ),
        aggregation_method=AggregationMethod.DIRECT_EQUAL_INDICATOR,
        contribution_mapping=ContributionMapping.TANH_SATURATION,
        contribution_mapping_parameters=ContributionMappingParameters(
            amplitude=Decimal(1), scale=Decimal(2)
        ),
        expected_constituent_count=expected,
        valid_constituent_count=valid,
        expected_weight=Decimal(expected),
        available_weight=Decimal(valid),
        coverage=resolved_confidence,
        constituent_signal_ids=definition.primary_signal_ids,
        constituent_observation_dates={
            signal_id: as_of if constituent_values[signal_id] is not None else None
            for signal_id in definition.primary_signal_ids
        },
        constituent_normalized_values=constituent_values,
        constituent_directed_values=constituent_values,
        constituent_pre_map_values=constituent_values,
        constituent_contributions=constituent_values,
        constituent_weights={signal_id: Decimal(1) for signal_id in definition.primary_signal_ids},
        constituent_statuses={
            signal_id: "OK" if constituent_values[signal_id] is not None else "MISSING"
            for signal_id in definition.primary_signal_ids
        },
        constituent_quality_flags={signal_id: [] for signal_id in definition.primary_signal_ids},
        constituent_calculation_hashes={
            signal_id: f"signal:{signal_id}:{revision}"
            if constituent_values[signal_id] is not None
            else None
            for signal_id in definition.primary_signal_ids
        },
        supporting_confirmation=[],
        concentration=ConcentrationMetadata(
            primary_count=expected,
            axis_count=1,
            largest_axis_share=Decimal(1),
            largest_redundancy_group_share=Decimal(1),
        ),
        source_registry_hash=COMPOSITE.required_source_registry_hash,
        feature_spec_version=COMPOSITE.required_feature_spec_version,
        normalization_spec_version=COMPOSITE.required_normalization_spec_version,
        normalization_spec_hash=COMPOSITE.required_normalization_spec_hash,
        is_point_in_time_safe=pit_safe,
        pillar_status=resolved_status,
        quality_flags=resolved_flags,
        calculation_hash=f"pillar:{pillar_id}:{as_of}:{value}:{revision}",
        revision_provenance_version=(REVISION_PROVENANCE.revision_provenance_version),
        revision_provenance_hash=REVISION_PROVENANCE.specification_hash,
        revision_evidence_hashes=(
            revision_effect.source_revision_evidence_hashes if revision_effect is not None else ()
        ),
        revision_effect=revision_effect,
    )


def _complete_rows(
    values: dict[str, Decimal] | None = None,
    *,
    as_of: date = AS_OF,
    pit_safe: bool = True,
) -> list[PillarResult]:
    resolved = {
        ACTIVITY: Decimal("-0.4"),
        LABOR: Decimal("-0.8"),
        INFLATION: Decimal("0.6"),
        MANUFACTURING: Decimal("-0.6"),
        HOUSING: Decimal("-0.2"),
        CONTEXT_IDS[0]: Decimal("0.1"),
        CONTEXT_IDS[1]: Decimal("0.2"),
        CONTEXT_IDS[2]: Decimal("0.3"),
        CONTEXT_IDS[3]: Decimal("0.4"),
    }
    resolved.update(values or {})
    return [
        _pillar(item.pillar_id, resolved[item.pillar_id], as_of=as_of, pit_safe=pit_safe)
        for item in PILLARS.pillars
    ]


def _engine(
    rows: list[PillarResult], *, cache: MacroStateResultCache | None = None
) -> tuple[MacroStateEngine, FakePillarEngine]:
    fake = FakePillarEngine(rows)
    engine = MacroStateEngine(
        Session(),
        pillar_catalog=PILLARS,
        composite_catalog=COMPOSITE,
        pillar_engine=fake,
        cache=cache if cache is not None else MacroStateResultCache(),
    )
    return engine, fake


@pytest.mark.asyncio
async def test_three_layer_state_uses_stage3c_once_and_has_no_headline_scalar() -> None:
    engine, fake = _engine(_complete_rows())
    result = await engine.compute_macro_state(AS_OF)
    assert fake.calls == [AS_OF]
    assert result.state_id == "US_MACRO__STATE_VECTOR"
    assert result.growth.component_id == "US_MACRO__GROWTH_STRENGTH"
    assert result.inflation.pillar_id == INFLATION
    assert [item.pillar_id for item in result.context] == CONTEXT_IDS
    assert not hasattr(result, "macro_score")
    assert not hasattr(result, "regime")
    assert not hasattr(result, "recession_probability")


@pytest.mark.asyncio
async def test_full_growth_has_equal_base_and_effective_weights() -> None:
    result = await _engine(_complete_rows())[0].compute_macro_state(AS_OF)
    assert result.growth.value == Decimal("-0.5")
    assert result.growth.coverage == result.growth.confidence == Decimal(1)
    assert result.growth.status is GrowthStatus.OK
    assert set(result.growth.pillar_base_weights.values()) == {Decimal("0.25")}
    assert set(result.growth.pillar_effective_weights.values()) == {Decimal("0.25")}
    assert result.macro_state_status is MacroStateStatus.OK


@pytest.mark.asyncio
async def test_2008_three_of_four_growth_is_partial_with_expected_confidence() -> None:
    as_of = date(2008, 12, 31)
    rows = _complete_rows(
        {
            ACTIVITY: Decimal("-0.45130362987655089688445863068639399485580711857118"),
            MANUFACTURING: Decimal("-0.8686927798160012543279016632332531668956735173493"),
            HOUSING: Decimal("-0.90886562199410164097814525327149438046444818673465"),
        },
        as_of=as_of,
    )
    rows = [
        _pillar(
            LABOR,
            None,
            as_of=as_of,
            status=PillarStatus.INSUFFICIENT_COVERAGE,
            confidence=Decimal(4) / Decimal(11),
        )
        if item.pillar_id == LABOR
        else _pillar(
            MANUFACTURING,
            item.value,
            as_of=as_of,
            status=PillarStatus.PARTIAL,
            confidence=Decimal("0.8"),
        )
        if item.pillar_id == MANUFACTURING
        else item
        for item in rows
    ]
    result = await _engine(rows)[0].compute_macro_state(as_of)
    expected_2008 = Decimal("-0.7429540105622179307301685157303805140719762742184")
    assert result.growth.value is not None
    assert abs(result.growth.value - expected_2008) < Decimal("1e-48")
    assert result.growth.coverage == Decimal("0.75")
    assert result.growth.confidence == Decimal("0.70")
    assert result.growth.status is GrowthStatus.PARTIAL
    assert result.growth.pillar_values[LABOR] is None
    assert result.growth.pillar_effective_weights[LABOR] == 0
    assert {
        result.growth.pillar_effective_weights[item] for item in GROWTH_IDS if item != LABOR
    } == {Decimal(1) / Decimal(3)}
    assert result.historical_coverage_state is HistoricalCoverageState.PARTIAL
    assert result.macro_state_status is MacroStateStatus.PARTIAL
    assert MacroStateQualityFlag.HISTORICAL_COVERAGE_LIMITATION in result.quality_flags
    assert result.is_point_in_time_safe


@pytest.mark.asyncio
async def test_two_of_four_growth_fails_quorum_without_imputation() -> None:
    rows = _complete_rows()
    rows = [
        _pillar(
            item.pillar_id,
            None,
            status=PillarStatus.INSUFFICIENT_COVERAGE,
            confidence=Decimal(0),
        )
        if item.pillar_id in {LABOR, HOUSING}
        else item
        for item in rows
    ]
    result = await _engine(rows)[0].compute_macro_state(AS_OF)
    assert result.growth.value is None
    assert result.growth.status is GrowthStatus.INSUFFICIENT_GROWTH_COVERAGE
    assert result.growth.coverage == Decimal("0.5")
    assert result.macro_state_status is MacroStateStatus.INSUFFICIENT_GROWTH_COVERAGE


@pytest.mark.asyncio
async def test_four_of_four_with_partial_input_remains_partial() -> None:
    rows = [
        _pillar(
            ACTIVITY,
            item.value,
            status=PillarStatus.PARTIAL,
            confidence=Decimal("0.75"),
        )
        if item.pillar_id == ACTIVITY
        else item
        for item in _complete_rows()
    ]
    result = await _engine(rows)[0].compute_macro_state(AS_OF)
    assert result.growth.value is not None
    assert result.growth.status is GrowthStatus.PARTIAL
    assert result.growth.coverage == 1
    assert result.growth.confidence == Decimal("0.9375")


@pytest.mark.asyncio
async def test_missing_inflation_preserves_growth_but_sets_overall_status() -> None:
    rows = [
        _pillar(item.pillar_id, None, status=PillarStatus.NO_VALID_SIGNALS)
        if item.pillar_id == INFLATION
        else item
        for item in _complete_rows()
    ]
    result = await _engine(rows)[0].compute_macro_state(AS_OF)
    assert result.growth.value == Decimal("-0.5")
    assert result.inflation.value is None
    assert result.macro_state_status is MacroStateStatus.MISSING_INFLATION


@pytest.mark.asyncio
async def test_missing_context_preserves_both_core_components() -> None:
    missing_id = CONTEXT_IDS[0]
    rows = [
        _pillar(item.pillar_id, None, status=PillarStatus.NO_VALID_SIGNALS)
        if item.pillar_id == missing_id
        else item
        for item in _complete_rows()
    ]
    result = await _engine(rows)[0].compute_macro_state(AS_OF)
    assert result.growth.value == Decimal("-0.5")
    assert result.inflation.value == Decimal("0.6")
    assert result.macro_state_status is MacroStateStatus.PARTIAL
    assert MacroStateQualityFlag.CONTEXT_PARTIAL in result.quality_flags
    assert next(item for item in result.context if item.pillar_id == missing_id).value is None


@pytest.mark.asyncio
async def test_context_values_never_modify_growth() -> None:
    baseline = await _engine(_complete_rows())[0].compute_macro_state(AS_OF)
    changed = await _engine(
        _complete_rows({pillar_id: Decimal("0.99") for pillar_id in CONTEXT_IDS})
    )[0].compute_macro_state(AS_OF)
    assert changed.growth.value == baseline.growth.value
    assert changed.growth.confidence == baseline.growth.confidence


@pytest.mark.asyncio
async def test_inflation_is_referenced_without_inversion_or_mapping() -> None:
    result = await _engine(_complete_rows({INFLATION: Decimal("0.799399")}))[0].compute_macro_state(
        AS_OF
    )
    assert result.inflation.value == Decimal("0.799399")
    assert result.inflation.semantic_direction == PILLARS.by_pillar()[INFLATION].semantic_direction


@pytest.mark.asyncio
async def test_status_precedence_growth_before_missing_inflation() -> None:
    rows = [
        _pillar(item.pillar_id, None, status=PillarStatus.NO_VALID_SIGNALS)
        if item.pillar_id in {LABOR, HOUSING, INFLATION}
        else item
        for item in _complete_rows()
    ]
    result = await _engine(rows)[0].compute_macro_state(AS_OF)
    assert result.macro_state_status is MacroStateStatus.INSUFFICIENT_GROWTH_COVERAGE


@pytest.mark.asyncio
async def test_as_of_mismatch_fails_closed_whole_state() -> None:
    rows = _complete_rows()
    rows[-1] = rows[-1].model_copy(update={"evaluation_as_of": date(2020, 5, 30)})
    result = await _engine(rows)[0].compute_macro_state(AS_OF)
    assert result.macro_state_status is MacroStateStatus.AS_OF_MISMATCH
    assert result.growth.value is None
    assert result.inflation.value is None
    assert all(item.value is None for item in result.context)


@pytest.mark.asyncio
async def test_pit_unsafe_context_rejects_whole_historical_state() -> None:
    rows = _complete_rows()
    index = next(index for index, item in enumerate(rows) if item.pillar_id == CONTEXT_IDS[0])
    rows[index] = rows[index].model_copy(update={"is_point_in_time_safe": False})
    result = await _engine(rows)[0].compute_macro_state(AS_OF)
    assert result.macro_state_status is MacroStateStatus.PIT_UNSAFE
    assert not result.is_point_in_time_safe
    assert result.growth.value is None
    assert result.inflation.value is None
    assert all(item.value is None for item in result.context)


@pytest.mark.asyncio
async def test_runtime_spec_mismatch_fails_closed() -> None:
    rows = _complete_rows()
    rows[0] = rows[0].model_copy(update={"pillar_spec_hash": "wrong"})
    result = await _engine(rows)[0].compute_macro_state(AS_OF)
    assert result.macro_state_status is MacroStateStatus.SPEC_MISMATCH
    assert result.growth.value is None


@pytest.mark.asyncio
async def test_true_upstream_error_uses_pillar_error_semantics() -> None:
    rows = _complete_rows()
    rows[0] = _pillar(ACTIVITY, None, status=PillarStatus.SIGNAL_ERROR)
    result = await _engine(rows)[0].compute_macro_state(AS_OF)
    assert result.macro_state_status is MacroStateStatus.PILLAR_ERROR
    assert result.growth.value is None


@pytest.mark.asyncio
async def test_incomplete_stage3c_inventory_is_pillar_error() -> None:
    result = await _engine(_complete_rows()[:-1])[0].compute_macro_state(AS_OF)
    assert result.macro_state_status is MacroStateStatus.PILLAR_ERROR
    assert result.growth.value is None


@pytest.mark.asyncio
async def test_breadth_dispersion_and_counts_are_diagnostic_only() -> None:
    values = {
        ACTIVITY: Decimal("-0.2"),
        LABOR: Decimal("0.2"),
        MANUFACTURING: Decimal("0"),
        HOUSING: Decimal("0"),
    }
    result = await _engine(_complete_rows(values))[0].compute_macro_state(AS_OF)
    assert result.growth.value == 0
    assert result.growth.positive_pillar_count == 1
    assert result.growth.negative_pillar_count == 1
    assert result.growth.neutral_pillar_count == 2
    assert result.growth.breadth == 0
    assert result.growth.dispersion == Decimal(
        "0.14142135623730950488016887242096980785696718753769"
    )
    assert result.growth.confidence == 1


@pytest.mark.asyncio
async def test_2020_and_2022_two_axis_controls_preserve_information() -> None:
    result_2020 = await _engine(
        _complete_rows(
            {
                ACTIVITY: Decimal("-0.49673118431388138568740326550470082250615372195365"),
                LABOR: Decimal("-0.86035208334606325702717284637694488538563730482557"),
                MANUFACTURING: Decimal("-0.90651597461170188939644877059466738236202100831442"),
                HOUSING: Decimal("-0.8647363368410939801670968723826685149838031280259"),
            }
        )
    )[0].compute_macro_state(AS_OF)
    result_2022 = await _engine(
        _complete_rows(
            {
                ACTIVITY: Decimal("-0.15749469665493563334153929713801917078885843500993"),
                LABOR: Decimal("0.2282619245404048445469289059551786044675233177452"),
                MANUFACTURING: Decimal("0.071411508116245440834822595570426099146523934208148"),
                HOUSING: Decimal("-0.47825562009241671082390245762121675495447315574052"),
                INFLATION: Decimal("0.79939948723228828909711763526019013397140190453297"),
            }
        )
    )[0].compute_macro_state(AS_OF)
    assert result_2020.growth.value == Decimal(
        "-0.78208389477818512806953043871474540130940379077988"
    )
    assert result_2022.growth.value == Decimal(
        "-0.084019221022675514695922563308407805532321084699275"
    )
    assert result_2022.inflation.value == Decimal(
        "0.79939948723228828909711763526019013397140190453297"
    )


@pytest.mark.asyncio
async def test_revision_changes_state_hash_and_propagates_only_real_flag() -> None:
    first_as_of = date(2020, 4, 29)
    second_as_of = date(2020, 5, 28)
    baseline_rows = _complete_rows(as_of=first_as_of)
    revised_rows = [
        _pillar(
            item.pillar_id,
            Decimal("-0.3") if item.pillar_id == ACTIVITY else item.value,
            as_of=second_as_of,
            revision=1 if item.pillar_id == ACTIVITY else 0,
            counterfactual_value=item.value if item.pillar_id == ACTIVITY else _UNSET,
        )
        for item in baseline_rows
    ]
    baseline = await _engine(baseline_rows)[0].compute_macro_state(first_as_of)
    revised = await _engine(revised_rows)[0].compute_macro_state(second_as_of)
    assert revised.growth.value != baseline.growth.value
    assert revised.calculation_hash != baseline.calculation_hash
    assert MacroStateQualityFlag.REVISION_CHANGED_RESULT in revised.quality_flags
    assert MacroStateQualityFlag.REVISION_CHANGED_RESULT not in baseline.quality_flags


@pytest.mark.asyncio
async def test_legacy_pillar_revision_flag_without_evidence_does_not_propagate() -> None:
    rows = _complete_rows()
    rows[0] = rows[0].model_copy(
        update={"quality_flags": [PillarQualityFlag.REVISION_CHANGED_RESULT]}
    )
    result = await _engine(rows)[0].compute_macro_state(AS_OF)
    assert MacroStateQualityFlag.REVISION_CHANGED_RESULT not in result.quality_flags
    assert result.revision_effect is None


@pytest.mark.asyncio
async def test_cache_is_deterministic_copy_safe_and_bounded() -> None:
    cache = MacroStateResultCache(max_entries=1)
    engine, fake = _engine(_complete_rows(), cache=cache)
    first = await engine.compute_macro_state(AS_OF)
    second = await engine.compute_macro_state(AS_OF)
    assert first == second
    assert first is not second
    assert first.calculation_hash == second.calculation_hash
    assert len(cache) == 1
    assert fake.calls == [AS_OF, AS_OF]


@pytest.mark.asyncio
async def test_current_and_pit_cache_contexts_are_isolated() -> None:
    cache = MacroStateResultCache(max_entries=2)
    current_rows = _complete_rows(as_of=date.today(), pit_safe=False)
    current = await _engine(current_rows, cache=cache)[0].compute_macro_state()
    pit = await _engine(_complete_rows(), cache=cache)[0].compute_macro_state(AS_OF)
    assert current.evaluation_mode is EvaluationMode.CURRENT
    assert current.revision_provenance_version is None
    assert current.revision_provenance_hash is None
    assert current.revision_effect is None
    assert pit.evaluation_mode is EvaluationMode.PIT_DATE_EOD
    assert current.calculation_hash != pit.calculation_hash
    assert len(cache) == 2


def test_public_domain_api_accepts_no_provider_weights_or_formulas() -> None:
    parameters = inspect.signature(compute_macro_state).parameters
    assert set(parameters) == {"session", "evaluation_as_of"}


def test_service_source_has_no_stage3e_or_lower_layer_access() -> None:
    source = inspect.getsource(MacroStateEngine)
    assert "NormalizedSignalResult" not in source
    assert "FeatureResult" not in source
    assert "Observation" not in source
    assert "regime" not in source.lower()
    assert "recession" not in source.lower()
