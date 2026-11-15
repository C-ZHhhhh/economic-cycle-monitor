"""Stage 3B service, provenance, direction, PIT, and cache tests."""

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from app.features.config import load_feature_catalog
from app.features.schemas import FeatureResult, FeatureStatus, QualityFlag
from app.signals.cache import SignalResultCache
from app.signals.schemas import SignalQualityFlag, SignalStatus
from app.signals.service import (
    SignalEngine,
    SignalNotFound,
    compute_feature_signals,
    compute_signal,
    compute_signal_set,
)
from app.source_registry.revision_provenance import (
    build_layer_effect,
    load_revision_provenance_catalog,
)
from app.source_registry.revision_schemas import RevisionEffectType

REGISTRY_HASH = "7a13095907733a1d6092ef5231c67de7528886c7eda9eeb73ed2166764834758"
FEATURES_BY_ID = load_feature_catalog().by_feature()


class FakeFeatureEngine:
    """In-memory Stage 3A boundary double; it exposes no provider observations."""

    def __init__(self, rows: list[FeatureResult]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, date, date, date | datetime | None]] = []

    async def compute_feature(
        self,
        feature_id: str,
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> list[FeatureResult]:
        self.calls.append((feature_id, start, end, as_of))
        return [
            item
            for item in self.rows
            if item.feature_id == feature_id and start <= item.feature_observation_date <= end
        ]


def _row(
    feature_id: str,
    observation_date: date,
    value: Decimal | None,
    *,
    as_of: date | None = None,
    status: FeatureStatus = FeatureStatus.OK,
    pit_safe: bool | None = None,
    registry_hash: str = REGISTRY_HASH,
    feature_spec_version: str = "stage3a-v1",
    flags: list[QualityFlag] | None = None,
    revision: int = 0,
    counterfactual_value: Decimal | None | object = ...,
) -> FeatureResult:
    indicator, feature = FEATURES_BY_ID[feature_id]
    safe = as_of is not None if pit_safe is None else pit_safe
    revision_effect = None
    if counterfactual_value is not ...:
        authority = load_revision_provenance_catalog()
        prior_value = counterfactual_value if isinstance(counterfactual_value, Decimal) else None
        revision_effect = build_layer_effect(
            authority=authority,
            source_evidence_hashes=("e" * 64,),
            source_effect_types=(RevisionEffectType.SOURCE_REVISION,),
            current_value=value,
            counterfactual_value=prior_value,
            current_status=status.value,
            counterfactual_status=status.value,
        )
    return FeatureResult(
        feature_id=feature_id,
        display_name=feature.display_name,
        description=feature.description,
        feature_spec_version=feature_spec_version,
        formula_registry_version="stage3a-formulas-v1",
        indicator_id=indicator.indicator_id,
        feature_observation_date=observation_date,
        as_of=as_of,
        value=value,
        unit=feature.output_unit,
        formula_id=feature.formula_id,
        window=feature.window,
        input_count=1,
        expected_input_count=1,
        input_observation_dates=[observation_date],
        input_sources=["canonical"],
        input_source_identities=["fixture"],
        input_mapping_versions=["test-v1"],
        input_release_dates=[observation_date],
        input_retrieved_at=[datetime(2026, 8, 27, tzinfo=UTC)],
        input_vintage_dates=[as_of],
        source_policy_version="stage2b5-v3",
        registry_hash=registry_hash,
        is_point_in_time_safe=safe,
        feature_status=status,
        quality_flags=flags or [],
        calculation_hash=(f"{feature_id}:{observation_date}:{value}:{revision}").encode().hex(),
        revision_provenance_version=(
            revision_effect.revision_provenance_version if revision_effect else None
        ),
        revision_provenance_hash=(
            revision_effect.revision_provenance_hash if revision_effect else None
        ),
        revision_evidence_hash="e" * 64 if revision_effect else None,
        revision_effect=revision_effect,
    )


def _monthly(
    feature_id: str,
    count: int,
    *,
    start: date = date(2018, 1, 1),
    as_of: date | None = None,
) -> list[FeatureResult]:
    return [
        _row(feature_id, start + relativedelta(months=index), Decimal(index), as_of=as_of)
        for index in range(count)
    ]


def _engine(
    rows: list[FeatureResult], *, cache: SignalResultCache | None = None
) -> tuple[SignalEngine, FakeFeatureEngine]:
    fake = FakeFeatureEngine(rows)
    return (
        SignalEngine(
            Session(),
            feature_engine=fake,
            cache=cache if cache is not None else SignalResultCache(),
        ),
        fake,
    )


@pytest.mark.asyncio
async def test_positive_direction_is_applied_after_prior_only_normalization() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    rows = _monthly(feature_id, 61)
    engine, _ = _engine(rows)
    target = rows[-1].feature_observation_date
    result = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert result.signal_status is SignalStatus.OK
    assert result.center == Decimal("29.5")
    assert result.normalized_value == result.directed_value
    assert result.history_count == 60
    assert result.history_end is not None
    assert result.history_end < result.feature_observation_date
    assert not result.distribution_includes_current


@pytest.mark.asyncio
async def test_inverse_direction_negates_only_normalized_value() -> None:
    feature_id = "US_INVENTORY_SALES_RATIO__LEVEL"
    rows = _monthly(feature_id, 61)
    engine, _ = _engine(rows)
    target = rows[-1].feature_observation_date
    result = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert result.signal_status is SignalStatus.OK
    assert result.normalized_value is not None
    assert result.directed_value == result.normalized_value.copy_negate()
    assert result.raw_feature_value == Decimal(60)


@pytest.mark.asyncio
async def test_context_only_produces_normalized_but_no_directed_value() -> None:
    feature_id = "US_PPI_COMMODITIES__LEVEL"
    rows = _monthly(feature_id, 61)
    engine, _ = _engine(rows)
    target = rows[-1].feature_observation_date
    result = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert result.signal_status is SignalStatus.OK
    assert result.normalized_value is not None
    assert result.directed_value is None
    assert SignalQualityFlag.CONTEXT_ONLY in result.quality_flags


@pytest.mark.asyncio
async def test_min_history_gate_and_partial_window() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    insufficient = _monthly(feature_id, 36)
    engine, _ = _engine(insufficient)
    target = insufficient[-1].feature_observation_date
    result = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert result.signal_status is SignalStatus.NORMALIZATION_INSUFFICIENT_HISTORY
    assert result.history_count == 35
    assert result.normalized_value is None

    partial = _monthly(feature_id, 37)
    engine, _ = _engine(partial)
    target = partial[-1].feature_observation_date
    result = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert result.signal_status is SignalStatus.OK
    assert result.history_count == 36
    assert SignalQualityFlag.PARTIAL_WINDOW in result.quality_flags


@pytest.mark.asyncio
async def test_missing_intermediate_feature_period_fails_closed() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    rows = _monthly(feature_id, 61)
    rows.pop(40)
    engine, _ = _engine(rows)
    target = rows[-1].feature_observation_date
    result = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert result.signal_status is SignalStatus.MISSING_FEATURE
    assert result.normalized_value is None


def _through(feature_id: str, last: date, *, as_of: date | None = None) -> list[FeatureResult]:
    """Monthly rows from 2019-01 through ``last`` with no row for October 2025."""
    rows: list[FeatureResult] = []
    current = date(2019, 1, 1)
    index = 0
    while current <= last:
        if current != date(2025, 10, 1):
            rows.append(_row(feature_id, current, Decimal(index), as_of=as_of))
        current += relativedelta(months=1)
        index += 1
    return rows


@pytest.mark.asyncio
async def test_declared_permanent_gap_is_skipped_without_imputation() -> None:
    feature_id = "US_UNEMPLOYMENT_RATE__LEVEL"
    engine, _ = _engine(_through(feature_id, date(2026, 7, 1)))
    result = (await engine.compute_feature_signals(feature_id, date(2026, 7, 1), date(2026, 7, 1)))[
        0
    ]
    assert result.signal_status is SignalStatus.OK
    assert result.history_count == 59
    assert SignalQualityFlag.PERMANENT_GAP_SKIPPED in result.quality_flags
    assert SignalQualityFlag.PARTIAL_WINDOW in result.quality_flags


@pytest.mark.asyncio
async def test_same_missing_month_still_fails_for_an_undeclared_indicator() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    engine, _ = _engine(_through(feature_id, date(2026, 7, 1)))
    result = (await engine.compute_feature_signals(feature_id, date(2026, 7, 1), date(2026, 7, 1)))[
        0
    ]
    assert result.signal_status is SignalStatus.MISSING_FEATURE
    assert SignalQualityFlag.PERMANENT_GAP_SKIPPED not in result.quality_flags


@pytest.mark.asyncio
async def test_history_features_blocked_only_by_the_declared_gap_are_skipped() -> None:
    feature_id = "US_PARTICIPATION_RATE__CHANGE_3M_PP"
    rows = _through(feature_id, date(2026, 7, 1))
    blocked = date(2026, 1, 1)
    rows = [
        item.model_copy(
            update={
                "value": None,
                "feature_status": FeatureStatus.MISSING_INPUT_PERIOD,
                "input_observation_dates": [date(2025, 11, 1), date(2025, 12, 1), blocked],
            }
        )
        if item.feature_observation_date == blocked
        else item
        for item in rows
    ]
    engine, _ = _engine(rows)
    result = (await engine.compute_feature_signals(feature_id, date(2026, 7, 1), date(2026, 7, 1)))[
        0
    ]
    assert result.signal_status is SignalStatus.OK
    assert SignalQualityFlag.PERMANENT_GAP_SKIPPED in result.quality_flags


@pytest.mark.asyncio
async def test_history_feature_missing_an_undeclared_period_still_fails_closed() -> None:
    feature_id = "US_PARTICIPATION_RATE__CHANGE_3M_PP"
    rows = _through(feature_id, date(2026, 7, 1))
    blocked = date(2024, 6, 1)
    rows = [
        item.model_copy(
            update={
                "value": None,
                "feature_status": FeatureStatus.MISSING_INPUT_PERIOD,
                "input_observation_dates": [date(2024, 4, 1), date(2024, 5, 1), blocked],
            }
        )
        if item.feature_observation_date == blocked
        else item
        for item in rows
    ]
    engine, _ = _engine(rows)
    result = (await engine.compute_feature_signals(feature_id, date(2026, 7, 1), date(2026, 7, 1)))[
        0
    ]
    assert result.signal_status is SignalStatus.FEATURE_ERROR


@pytest.mark.asyncio
async def test_pit_evaluation_before_the_gap_was_confirmed_does_not_skip_it() -> None:
    feature_id = "US_UNEMPLOYMENT_RATE__LEVEL"
    early = date(2025, 12, 1)
    engine, _ = _engine(_through(feature_id, date(2025, 11, 1), as_of=early))
    before = (
        await engine.compute_feature_signals(
            feature_id, date(2025, 11, 1), date(2025, 11, 1), early
        )
    )[0]
    assert before.signal_status is SignalStatus.MISSING_FEATURE

    later = date(2026, 1, 15)
    engine, _ = _engine(_through(feature_id, date(2025, 12, 1), as_of=later))
    after = (
        await engine.compute_feature_signals(
            feature_id, date(2025, 12, 1), date(2025, 12, 1), later
        )
    )[0]
    assert after.signal_status is SignalStatus.OK
    assert SignalQualityFlag.PERMANENT_GAP_SKIPPED in after.quality_flags


@pytest.mark.asyncio
async def test_results_that_never_meet_a_gap_keep_their_calculation_identity() -> None:
    feature_id = "US_UNEMPLOYMENT_RATE__LEVEL"
    rows = _monthly(feature_id, 61)
    target = rows[-1].feature_observation_date
    declared, _ = _engine(rows)
    empty = declared.permanent_gaps.model_copy(update={"gaps": [], "permanent_gap_hash": "0" * 64})
    undeclared = SignalEngine(
        Session(), feature_engine=FakeFeatureEngine(rows), permanent_gaps=empty
    )
    first = (await declared.compute_feature_signals(feature_id, target, target))[0]
    second = (await undeclared.compute_feature_signals(feature_id, target, target))[0]
    assert first.signal_status is SignalStatus.OK
    assert first.calculation_hash == second.calculation_hash


@pytest.mark.asyncio
async def test_feature_error_and_pit_unsafe_fail_closed() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    rows = _monthly(feature_id, 61)
    rows[-1] = rows[-1].model_copy(
        update={"feature_status": FeatureStatus.INVALID_DENOMINATOR, "value": None}
    )
    engine, _ = _engine(rows)
    target = rows[-1].feature_observation_date
    assert (await engine.compute_feature_signals(feature_id, target, target))[
        0
    ].signal_status is SignalStatus.FEATURE_ERROR

    as_of = date(2024, 1, 7)
    pit_rows = _monthly(feature_id, 61, as_of=as_of)
    pit_rows[20] = pit_rows[20].model_copy(update={"is_point_in_time_safe": False})
    engine, _ = _engine(pit_rows)
    target = pit_rows[-1].feature_observation_date
    assert (await engine.compute_feature_signals(feature_id, target, target, as_of))[
        0
    ].signal_status is SignalStatus.PIT_UNSAFE


@pytest.mark.asyncio
async def test_registry_and_feature_spec_mismatch_fail_closed() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    rows = _monthly(feature_id, 61)
    rows[-1] = rows[-1].model_copy(update={"registry_hash": "0" * 64})
    engine, _ = _engine(rows)
    target = rows[-1].feature_observation_date
    assert (await engine.compute_feature_signals(feature_id, target, target))[
        0
    ].signal_status is SignalStatus.REGISTRY_HASH_MISMATCH

    rows = _monthly(feature_id, 61)
    rows[-1] = rows[-1].model_copy(update={"feature_spec_version": "future"})
    engine, _ = _engine(rows)
    target = rows[-1].feature_observation_date
    assert (await engine.compute_feature_signals(feature_id, target, target))[
        0
    ].signal_status is SignalStatus.FEATURE_SPEC_MISMATCH


@pytest.mark.asyncio
async def test_feature_quality_flags_and_provenance_are_preserved() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    rows = _monthly(feature_id, 61)
    rows[-1] = rows[-1].model_copy(
        update={"quality_flags": [QualityFlag.FALLBACK_INPUT, QualityFlag.DEGRADED_INPUT]}
    )
    engine, _ = _engine(rows)
    target = rows[-1].feature_observation_date
    result = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert SignalQualityFlag.FALLBACK_INPUT in result.quality_flags
    assert SignalQualityFlag.DEGRADED_INPUT in result.quality_flags
    assert result.feature_calculation_hash == rows[-1].calculation_hash
    assert result.input_feature_calculation_hashes[-1] == rows[-1].calculation_hash
    assert result.history_feature_calculation_hash
    assert result.normalization_spec_hash
    assert result.calculation_hash
    assert result.window_coverage == Decimal(1)


@pytest.mark.asyncio
async def test_current_and_pit_use_same_service_path_but_remain_isolated() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    current_rows = _monthly(feature_id, 61)
    as_of = date(2024, 1, 7)
    pit_rows = [
        row.model_copy(update={"as_of": as_of, "is_point_in_time_safe": True})
        for row in current_rows
    ]
    fake = FakeFeatureEngine(current_rows)
    engine = SignalEngine(Session(), feature_engine=fake)
    target = current_rows[-1].feature_observation_date
    current = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert current.revision_provenance_version is None
    assert current.revision_provenance_hash is None
    assert current.revision_effect is None
    fake.rows = pit_rows
    pit = (await engine.compute_feature_signals(feature_id, target, target, as_of))[0]
    assert current.as_of is None and not current.is_point_in_time_safe
    assert pit.as_of == as_of and pit.is_point_in_time_safe
    assert current.normalized_value == pit.normalized_value
    assert current.calculation_hash != pit.calculation_hash


@pytest.mark.asyncio
async def test_repeat_is_deterministic_and_cache_is_bounded() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    rows = _monthly(feature_id, 61)
    cache = SignalResultCache(max_entries=1)
    engine, _ = _engine(rows, cache=cache)
    target = rows[-1].feature_observation_date
    first = (await engine.compute_feature_signals(feature_id, target, target))[0]
    second = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert first.calculation_hash == second.calculation_hash
    assert len(cache) == 1


@pytest.mark.asyncio
async def test_feature_revision_invalidates_cache_and_changes_distribution() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    rows = _monthly(feature_id, 61)
    cache = SignalResultCache()
    engine, fake = _engine(rows, cache=cache)
    target = rows[-1].feature_observation_date
    first = (await engine.compute_feature_signals(feature_id, target, target))[0]
    fake.rows[-2] = _row(
        feature_id,
        fake.rows[-2].feature_observation_date,
        Decimal(500),
        revision=1,
        counterfactual_value=Decimal(59),
    )
    second = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert first.center != second.center
    assert first.calculation_hash != second.calculation_hash
    assert SignalQualityFlag.REVISION_CHANGED_DISTRIBUTION in second.quality_flags
    assert len(cache) == 2


@pytest.mark.asyncio
async def test_target_revision_does_not_claim_prior_distribution_changed() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    rows = _monthly(feature_id, 61)
    engine, fake = _engine(rows, cache=SignalResultCache())
    target = rows[-1].feature_observation_date
    first = (await engine.compute_feature_signals(feature_id, target, target))[0]
    fake.rows[-1] = _row(
        feature_id,
        target,
        Decimal(500),
        revision=1,
        counterfactual_value=Decimal(60),
    )
    second = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert first.center == second.center
    assert first.scale == second.scale
    assert first.normalized_value != second.normalized_value
    assert SignalQualityFlag.REVISION_CHANGED_RESULT in second.quality_flags
    assert SignalQualityFlag.REVISION_CHANGED_DISTRIBUTION not in second.quality_flags


@pytest.mark.asyncio
async def test_extreme_covid_style_value_is_not_clipped() -> None:
    feature_id = "US_NONFARM_EMPLOYMENT__CHANGE_1M"
    rows = _monthly(feature_id, 121)
    rows[-1] = _row(feature_id, rows[-1].feature_observation_date, Decimal(-20000))
    engine, _ = _engine(rows)
    target = rows[-1].feature_observation_date
    result = (await engine.compute_feature_signals(feature_id, target, target))[0]
    assert result.signal_status is SignalStatus.OK
    assert result.normalized_value is not None and result.normalized_value < -4
    assert result.directed_value == result.normalized_value
    assert SignalQualityFlag.EXTREME_VALUE in result.quality_flags


@pytest.mark.asyncio
async def test_signal_set_deduplicates_same_feature_request() -> None:
    feature_id = "US_SAVING_RATE__LEVEL"
    rows = _monthly(feature_id, 61)
    engine, fake = _engine(rows)
    definition = engine.signal_catalog.by_feature()[feature_id]
    target = rows[-1].feature_observation_date
    result = await engine.compute_signal_set(
        [definition.signal_id, definition.signal_id], target, target
    )
    assert len(result) == 1
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_unknown_ids_and_invalid_ranges_fail_closed() -> None:
    engine, _ = _engine([])
    with pytest.raises(SignalNotFound, match="UNKNOWN_SIGNAL"):
        await engine.compute_signal("missing", date(2024, 1, 1), date(2024, 1, 1))
    with pytest.raises(SignalNotFound, match="UNKNOWN_FEATURE"):
        await engine.compute_feature_signals("missing", date(2024, 1, 1), date(2024, 1, 1))
    signal_id = next(iter(engine.signal_catalog.by_signal()))
    with pytest.raises(ValueError, match="start"):
        await engine.compute_signal(signal_id, date(2024, 2, 1), date(2024, 1, 1))
    with pytest.raises(ValueError, match="PIT_GRANULARITY"):
        await engine.compute_signal(
            signal_id,
            date(2024, 1, 1),
            date(2024, 1, 1),
            datetime(2024, 1, 1, tzinfo=UTC),
        )


def test_public_domain_api_accepts_no_provider_scoring_or_formula_arguments() -> None:
    for function in (compute_signal, compute_feature_signals, compute_signal_set):
        parameters = inspect.signature(function).parameters
        for forbidden in ("provider", "source", "weight", "pillar", "score", "formula"):
            assert forbidden not in parameters


def test_stage3b_service_imports_no_provider_or_persistence_models() -> None:
    source = Path("app/signals/service.py").read_text(encoding="utf-8")
    for forbidden in (
        "RawObservation",
        "ObservationVintage",
        "DerivedFeature",
        "app.data_sources",
        "app.data_pipeline",
    ):
        assert forbidden not in source
