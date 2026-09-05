"""Stage 3C aggregation, selection, PIT, provenance, and cache tests."""

import inspect
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.pillars.cache import PillarResultCache
from app.pillars.calculations import decimal_tanh
from app.pillars.config import load_pillar_catalog
from app.pillars.schemas import (
    PillarQualityFlag,
    PillarRole,
    PillarStatus,
    SupportingConfirmationStatus,
)
from app.pillars.service import (
    PillarEngine,
    PillarNotFound,
    compute_all_pillars,
    compute_pillar,
    compute_pillar_set,
)
from app.signals.config import load_signal_catalog
from app.signals.schemas import (
    NormalizedSignalResult,
    SignalQualityFlag,
    SignalStatus,
)
from app.source_registry.revision_provenance import (
    build_layer_effect,
    load_revision_provenance_catalog,
)
from app.source_registry.revision_schemas import RevisionEffectType

SIGNALS = load_signal_catalog()
PILLARS = load_pillar_catalog(signal_catalog=SIGNALS)
REGISTRY_HASH = "7a13095907733a1d6092ef5231c67de7528886c7eda9eeb73ed2166764834758"
ACTIVITY = "US_MACRO__REAL_ACTIVITY_AND_DEMAND"
INFLATION = "US_MACRO__INFLATION_PRESSURE"


class FakeSignalEngine:
    """In-memory Stage 3B boundary double with no feature or provider access."""

    def __init__(self, rows: list[NormalizedSignalResult], *, fail: bool = False) -> None:
        self.rows = rows
        self.fail = fail
        self.calls: list[tuple[list[str], date, date, date | datetime | None]] = []

    async def compute_signal_set(
        self,
        signal_ids: Sequence[str],
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> list[NormalizedSignalResult]:
        self.calls.append((list(signal_ids), start, end, as_of))
        if self.fail:
            raise RuntimeError("fixture signal failure")
        return [
            row
            for row in self.rows
            if row.signal_id in signal_ids and start <= row.feature_observation_date <= end
        ]


def _row(
    signal_id: str,
    directed: Decimal | None,
    *,
    observation_date: date = date(2026, 8, 1),
    as_of: date | None = None,
    status: SignalStatus = SignalStatus.OK,
    pit_safe: bool | None = None,
    normalization_hash: str | None = None,
    flags: list[SignalQualityFlag] | None = None,
    revision: int = 0,
    counterfactual_directed: Decimal | None | object = ...,
) -> NormalizedSignalResult:
    definition = SIGNALS.by_signal()[signal_id]
    safe = as_of is not None if pit_safe is None else pit_safe
    value = directed if status is SignalStatus.OK else None
    revision_effect = None
    if counterfactual_directed is not ...:
        authority = load_revision_provenance_catalog()
        prior_value = (
            counterfactual_directed if isinstance(counterfactual_directed, Decimal) else None
        )
        revision_effect = build_layer_effect(
            authority=authority,
            source_evidence_hashes=("e" * 64,),
            source_effect_types=(RevisionEffectType.SOURCE_REVISION,),
            current_value=value,
            counterfactual_value=prior_value,
            current_status=status.value,
            counterfactual_status=status.value,
        )
    quality_flags = list(flags or [])
    if revision_effect is not None and revision_effect.result_changed:
        quality_flags.append(SignalQualityFlag.REVISION_CHANGED_RESULT)
    return NormalizedSignalResult(
        signal_id=signal_id,
        feature_id=definition.feature_id,
        feature_spec_version="stage3a-v1",
        normalization_spec_version="stage3b-v1",
        normalization_spec_hash=normalization_hash or SIGNALS.specification_hash,
        indicator_id=definition.indicator_id,
        economic_axis=definition.economic_axis,
        feature_role=definition.feature_role,
        redundancy_group=definition.redundancy_group,
        feature_observation_date=observation_date,
        as_of=as_of,
        raw_feature_value=value,
        raw_feature_unit="fixture",
        normalized_value=value,
        directed_value=value,
        normalized_unit="z_score",
        normalization_method=definition.normalization_method,
        window=definition.full_window,
        min_history=definition.min_history,
        history_start=date(2015, 1, 1),
        history_end=date(2024, 12, 1),
        history_count=definition.full_window,
        window_coverage=Decimal(1),
        center=Decimal(0),
        scale=Decimal(1),
        dispersion_estimator="fixture",
        distribution_includes_current=False,
        direction=definition.direction,
        direction_multiplier=definition.direction_multiplier,
        source_policy_version="stage2b5-v3",
        registry_hash=REGISTRY_HASH,
        feature_calculation_hash=f"feature:{signal_id}:{observation_date}:{revision}",
        input_feature_calculation_hashes=[f"feature:{signal_id}:{revision}"],
        history_feature_calculation_hash=f"history:{signal_id}:{revision}",
        is_point_in_time_safe=safe,
        signal_status=status,
        quality_flags=quality_flags,
        calculation_hash=f"signal:{signal_id}:{observation_date}:{as_of}:{directed}:{revision}",
        revision_provenance_version=(
            revision_effect.revision_provenance_version if revision_effect else None
        ),
        revision_provenance_hash=(
            revision_effect.revision_provenance_hash if revision_effect else None
        ),
        revision_evidence_hashes=revision_effect.source_revision_evidence_hashes
        if revision_effect
        else (),
        revision_effect=revision_effect,
    )


def _primary_rows(
    pillar_id: str,
    values: Sequence[Decimal],
    *,
    observation_date: date = date(2026, 8, 1),
    as_of: date | None = None,
    revision: int = 0,
    counterfactual_values: Sequence[Decimal | None | object] | None = None,
) -> list[NormalizedSignalResult]:
    signal_ids = PILLARS.by_pillar()[pillar_id].primary_signal_ids
    assert len(signal_ids) == len(values)
    prior_values = counterfactual_values or [...] * len(values)
    return [
        _row(
            signal_id,
            value,
            observation_date=observation_date,
            as_of=as_of,
            revision=revision,
            counterfactual_directed=counterfactual,
        )
        for signal_id, value, counterfactual in zip(signal_ids, values, prior_values, strict=True)
    ]


def _engine(
    rows: list[NormalizedSignalResult], *, cache: PillarResultCache | None = None
) -> tuple[PillarEngine, FakeSignalEngine]:
    fake = FakeSignalEngine(rows)
    engine = PillarEngine(
        Session(),
        signal_catalog=SIGNALS,
        pillar_catalog=PILLARS,
        signal_engine=fake,
        cache=cache if cache is not None else PillarResultCache(),
    )
    return engine, fake


@pytest.mark.asyncio
async def test_direct_equal_indicator_aggregation_and_audit_fields() -> None:
    values = [Decimal("1"), Decimal("-1"), Decimal("2"), Decimal("-2")]
    engine, fake = _engine(_primary_rows(ACTIVITY, values))
    result = await engine.compute_pillar(ACTIVITY)
    assert result.value == Decimal(0)
    assert result.pillar_status is PillarStatus.OK
    assert result.expected_constituent_count == result.valid_constituent_count == 4
    assert result.coverage == result.confidence == Decimal(1)
    assert set(result.constituent_weights.values()) == {Decimal(1)}
    assert result.expected_weight == result.available_weight == Decimal(4)
    assert result.unit == "bounded_symmetric_score"
    assert result.aggregation_method.value == "DIRECT_EQUAL_INDICATOR"
    assert result.contribution_mapping_parameters.scale == Decimal(2)
    definition = PILLARS.by_pillar()[ACTIVITY]
    assert len(fake.calls) == len(definition.primary_signal_ids) + len(
        definition.supporting_signals
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("valid_count", "status", "has_value"),
    [
        (4, PillarStatus.OK, True),
        (3, PillarStatus.PARTIAL, True),
        (2, PillarStatus.INSUFFICIENT_COVERAGE, False),
        (0, PillarStatus.NO_VALID_SIGNALS, False),
    ],
)
async def test_coverage_controls_value_without_imputing_zero(
    valid_count: int, status: PillarStatus, has_value: bool
) -> None:
    definition = PILLARS.by_pillar()[ACTIVITY]
    rows = [
        _row(signal_id, Decimal(index + 1))
        for index, signal_id in enumerate(definition.primary_signal_ids[:valid_count])
    ]
    result = await _engine(rows)[0].compute_pillar(ACTIVITY)
    assert result.valid_constituent_count == valid_count
    assert result.pillar_status is status
    assert (result.value is not None) is has_value
    assert result.confidence == result.coverage
    assert result.confidence != abs(result.value or Decimal(0)) or result.coverage == Decimal(0)
    if valid_count < 4:
        assert PillarQualityFlag.LOW_COVERAGE in result.quality_flags


@pytest.mark.asyncio
async def test_latest_error_never_falls_back_to_older_ok_signal() -> None:
    definition = PILLARS.by_pillar()[ACTIVITY]
    rows = _primary_rows(ACTIVITY, [Decimal(1)] * 4, observation_date=date(2026, 6, 1))
    rows.append(
        _row(
            definition.primary_signal_ids[0],
            None,
            observation_date=date(2026, 7, 1),
            status=SignalStatus.FEATURE_ERROR,
        )
    )
    result = await _engine(rows)[0].compute_pillar(ACTIVITY)
    signal_id = definition.primary_signal_ids[0]
    assert result.constituent_observation_dates[signal_id] == date(2026, 7, 1)
    assert result.constituent_statuses[signal_id] == SignalStatus.FEATURE_ERROR.value
    assert result.constituent_contributions[signal_id] is None
    assert result.valid_constituent_count == 3
    assert result.pillar_status is PillarStatus.PARTIAL


@pytest.mark.asyncio
async def test_mixed_frequency_selects_latest_known_native_dates_without_resampling() -> None:
    definition = PILLARS.by_pillar()[ACTIVITY]
    dates = [date(2026, 4, 1), date(2026, 6, 1), date(2026, 8, 1), date(2026, 8, 1)]
    rows = [
        _row(signal_id, Decimal(1), observation_date=observation_date)
        for signal_id, observation_date in zip(definition.primary_signal_ids, dates, strict=True)
    ]
    result = await _engine(rows)[0].compute_pillar(ACTIVITY)
    assert list(result.constituent_observation_dates.values()) == dates
    assert result.pillar_status is PillarStatus.OK


@pytest.mark.asyncio
async def test_future_signal_is_not_visible_before_shared_eod() -> None:
    as_of = date(2025, 1, 31)
    rows = _primary_rows(
        ACTIVITY,
        [Decimal(1)] * 4,
        observation_date=date(2025, 1, 1),
        as_of=as_of,
    )
    rows.append(
        _row(
            PILLARS.by_pillar()[ACTIVITY].primary_signal_ids[0],
            Decimal(9),
            observation_date=date(2025, 2, 1),
            as_of=as_of,
        )
    )
    engine, _ = _engine(rows)
    result = await engine.compute_pillar(ACTIVITY, as_of)
    assert result.constituent_observation_dates[
        PILLARS.by_pillar()[ACTIVITY].primary_signal_ids[0]
    ] == date(2025, 1, 1)
    assert result.is_point_in_time_safe
    assert result.pillar_status is PillarStatus.OK


@pytest.mark.asyncio
async def test_same_eod_pit_is_safe_and_current_pit_mixing_fails_closed() -> None:
    as_of = date(2025, 2, 15)
    safe_rows = _primary_rows(
        ACTIVITY, [Decimal(1)] * 4, observation_date=date(2025, 1, 1), as_of=as_of
    )
    safe_result = await _engine(safe_rows)[0].compute_pillar(ACTIVITY, as_of)
    assert safe_result.pillar_status is PillarStatus.OK
    assert safe_result.is_point_in_time_safe

    mixed = list(safe_rows)
    mixed[-1] = mixed[-1].model_copy(update={"as_of": None, "is_point_in_time_safe": False})
    unsafe = await _engine(mixed)[0].compute_pillar(ACTIVITY, as_of)
    assert unsafe.pillar_status is PillarStatus.PIT_UNSAFE
    assert unsafe.value is None
    assert not unsafe.is_point_in_time_safe


@pytest.mark.asyncio
async def test_current_evaluation_rejects_historical_pit_rows() -> None:
    rows = _primary_rows(
        ACTIVITY,
        [Decimal(1)] * 4,
        observation_date=date(2026, 8, 1),
        as_of=date(2026, 8, 15),
    )
    result = await _engine(rows)[0].compute_pillar(ACTIVITY)
    assert result.pillar_status is PillarStatus.PIT_UNSAFE
    assert result.value is None


@pytest.mark.asyncio
async def test_spec_mismatch_nulls_the_pillar() -> None:
    rows = _primary_rows(ACTIVITY, [Decimal(1)] * 4)
    rows[0] = rows[0].model_copy(update={"normalization_spec_hash": "wrong"})
    result = await _engine(rows)[0].compute_pillar(ACTIVITY)
    assert result.pillar_status is PillarStatus.SPEC_MISMATCH
    assert result.value is None


@pytest.mark.asyncio
async def test_extreme_contribution_saturates_without_clip_or_confidence_penalty() -> None:
    rows = _primary_rows(ACTIVITY, [Decimal(10), Decimal(0), Decimal(0), Decimal(0)])
    result = await _engine(rows)[0].compute_pillar(ACTIVITY)
    signal_id = PILLARS.by_pillar()[ACTIVITY].primary_signal_ids[0]
    assert result.constituent_pre_map_values[signal_id] == Decimal(10)
    contribution = result.constituent_contributions[signal_id]
    assert contribution is not None and Decimal(0) < contribution < Decimal(1)
    assert result.value is not None
    assert abs(result.value - contribution / Decimal(4)) < Decimal("1e-27")
    assert result.confidence == Decimal(1)
    assert PillarQualityFlag.EXTREME_CONTRIBUTION in result.quality_flags


@pytest.mark.asyncio
async def test_supporting_divergence_changes_diagnostics_but_never_score() -> None:
    primary_rows = _primary_rows(ACTIVITY, [Decimal(1)] * 4)
    gdp_supporting = [
        item
        for item in PILLARS.by_pillar()[ACTIVITY].supporting_signals
        if item.redundancy_group == "GDP_HORIZONS"
    ]
    confirming = primary_rows + [_row(item.signal_id, Decimal(1)) for item in gdp_supporting]
    diverging = primary_rows + [_row(item.signal_id, Decimal(-1)) for item in gdp_supporting]
    confirms = await _engine(confirming)[0].compute_pillar(ACTIVITY)
    diverges = await _engine(diverging)[0].compute_pillar(ACTIVITY)
    assert confirms.value == diverges.value
    assert confirms.confidence == diverges.confidence == Decimal(1)
    assert confirms.supporting_confirmation[0].status is (
        SupportingConfirmationStatus.CONFIRMS_PRIMARY
    )
    assert diverges.supporting_confirmation[0].status is (
        SupportingConfirmationStatus.DIVERGES_FROM_PRIMARY
    )
    assert PillarQualityFlag.DIVERGENT_SUPPORTING_SIGNALS in diverges.quality_flags


@pytest.mark.asyncio
async def test_participation_and_ahe_are_one_equal_indicator_each() -> None:
    labor = PILLARS.by_pillar()["US_MACRO__LABOR_STRENGTH"]
    inflation = PILLARS.by_pillar()[INFLATION]
    labor_result = await _engine(_primary_rows(labor.pillar_id, [Decimal(1)] * 11))[
        0
    ].compute_pillar(labor.pillar_id)
    inflation_result = await _engine(_primary_rows(inflation.pillar_id, [Decimal(1)] * 6))[
        0
    ].compute_pillar(inflation.pillar_id)
    participation = next(item for item in labor.primary_signal_ids if "PARTICIPATION" in item)
    ahe = next(item for item in inflation.primary_signal_ids if "HOURLY_EARNINGS" in item)
    assert labor_result.constituent_weights[participation] == Decimal(1)
    assert labor_result.expected_weight == Decimal(11)
    assert inflation_result.constituent_weights[ahe] == Decimal(1)
    assert inflation_result.expected_weight == Decimal(6)
    assert inflation_result.semantic_direction == "higher means greater inflation pressure"


@pytest.mark.asyncio
async def test_contextual_pillars_are_typed_and_not_core_eligible() -> None:
    pillar_id = "US_MACRO__HOUSEHOLD_FINANCIAL_CUSHION"
    result = await _engine(_primary_rows(pillar_id, [Decimal(1)]))[0].compute_pillar(pillar_id)
    assert result.pillar_role is PillarRole.CONTEXTUAL
    assert not result.core_composite_eligible
    assert result.value == decimal_tanh(Decimal(1))


@pytest.mark.asyncio
async def test_upstream_failure_is_a_constituent_error_without_zero_imputation() -> None:
    fake = FakeSignalEngine([], fail=True)
    engine = PillarEngine(
        Session(),
        signal_catalog=SIGNALS,
        pillar_catalog=PILLARS,
        signal_engine=fake,
    )
    result = await engine.compute_pillar(ACTIVITY)
    assert result.pillar_status is PillarStatus.NO_VALID_SIGNALS
    assert result.value is None
    assert set(result.constituent_statuses.values()) == {"SIGNAL_ERROR"}


@pytest.mark.asyncio
async def test_one_signal_exception_does_not_rollback_other_constituents() -> None:
    definition = PILLARS.by_pillar()[ACTIVITY]
    rows = _primary_rows(ACTIVITY, [Decimal(1)] * 4)

    class OneFailureSignalEngine(FakeSignalEngine):
        async def compute_signal_set(
            self,
            signal_ids: Sequence[str],
            start: date,
            end: date,
            as_of: date | datetime | None = None,
        ) -> list[NormalizedSignalResult]:
            if signal_ids == [definition.primary_signal_ids[0]]:
                raise RuntimeError("one fixture signal is unavailable")
            return await super().compute_signal_set(signal_ids, start, end, as_of)

    fake = OneFailureSignalEngine(rows)
    engine = PillarEngine(
        Session(),
        signal_catalog=SIGNALS,
        pillar_catalog=PILLARS,
        signal_engine=fake,
    )
    result = await engine.compute_pillar(ACTIVITY)
    assert result.pillar_status is PillarStatus.PARTIAL
    assert result.valid_constituent_count == 3
    assert result.value is not None
    assert result.constituent_statuses[definition.primary_signal_ids[0]] == "SIGNAL_ERROR"
    assert result.constituent_quality_flags[definition.primary_signal_ids[0]] == []


@pytest.mark.asyncio
async def test_revision_changes_pillar_value_and_hash_for_same_economic_period() -> None:
    first_as_of = date(2025, 1, 15)
    second_as_of = date(2025, 2, 15)
    observation_date = date(2024, 10, 1)
    rows = _primary_rows(
        ACTIVITY,
        [Decimal(1)] * 4,
        observation_date=observation_date,
        as_of=first_as_of,
    )
    engine, fake = _engine(rows)
    first = await engine.compute_pillar(ACTIVITY, first_as_of)
    fake.rows = _primary_rows(
        ACTIVITY,
        [Decimal(2), Decimal(1), Decimal(1), Decimal(1)],
        observation_date=observation_date,
        as_of=second_as_of,
        revision=1,
        counterfactual_values=[Decimal(1), ..., ..., ...],
    )
    second = await engine.compute_pillar(ACTIVITY, second_as_of)
    assert first.value != second.value
    assert first.calculation_hash != second.calculation_hash
    assert PillarQualityFlag.REVISION_CHANGED_RESULT in second.quality_flags
    revised_id = PILLARS.by_pillar()[ACTIVITY].primary_signal_ids[0]
    assert "REVISION_CHANGED_RESULT" in second.constituent_quality_flags[revised_id]
    unchanged_ids = PILLARS.by_pillar()[ACTIVITY].primary_signal_ids[1:]
    assert all(
        first.constituent_directed_values[item] == second.constituent_directed_values[item]
        for item in unchanged_ids
    )
    assert all(
        "REVISION_CHANGED_RESULT" not in second.constituent_quality_flags[item]
        for item in unchanged_ids
    )
    assert second.confidence_components.revision_state == "CHANGED"


@pytest.mark.asyncio
async def test_calculation_and_cache_are_deterministic_and_bounded() -> None:
    cache = PillarResultCache(max_entries=1)
    engine, fake = _engine(_primary_rows(ACTIVITY, [Decimal(1)] * 4), cache=cache)
    first = await engine.compute_pillar(ACTIVITY)
    second = await engine.compute_pillar(ACTIVITY)
    assert first == second
    assert first.calculation_hash == second.calculation_hash
    assert len(cache) == 1
    definition = PILLARS.by_pillar()[ACTIVITY]
    expected_calls = len(definition.primary_signal_ids) + len(definition.supporting_signals)
    assert len(fake.calls) == 2 * expected_calls
    await engine.compute_pillar(INFLATION)
    assert len(cache) == 1


@pytest.mark.asyncio
async def test_all_nine_pillars_compute_without_a_composite() -> None:
    rows = [
        _row(signal_id, Decimal(0))
        for pillar in PILLARS.pillars
        for signal_id in pillar.primary_signal_ids
    ]
    results = await _engine(rows)[0].compute_all_pillars()
    assert len(results) == 9
    assert all(item.revision_provenance_version is None for item in results)
    assert all(item.revision_provenance_hash is None for item in results)
    assert all(item.revision_effect is None for item in results)
    assert {item.pillar_id for item in results} == set(PILLARS.by_pillar())
    assert all(item.value == Decimal(0) for item in results)


@pytest.mark.asyncio
async def test_unknown_pillar_fails_closed() -> None:
    with pytest.raises(PillarNotFound, match="UNKNOWN_PILLAR"):
        await _engine([])[0].compute_pillar("UNKNOWN")


def test_public_domain_api_has_no_provider_weight_formula_or_regime_inputs() -> None:
    for function in (compute_pillar, compute_pillar_set, compute_all_pillars):
        parameters = set(inspect.signature(function).parameters)
        assert not parameters.intersection(
            {"provider", "raw_observations", "weights", "formula", "regime_threshold"}
        )
