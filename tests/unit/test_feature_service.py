"""Offline domain-service tests for current/PIT features, provenance, and cache."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from app.features.cache import FeatureCacheKey, FeatureResultCache
from app.features.config import load_feature_catalog
from app.features.schemas import FeatureStatus, QualityFlag
from app.features.service import FeatureEngine, FeatureNotFound, FeatureRegistryMismatch
from app.source_registry.config import load_source_registry
from app.source_registry.resolver import PITGranularityUnsupported, SourceIntegrityError
from app.source_registry.schemas import EffectiveObservation, MappingQuality, RuntimeHealth


def _monthly_dates(start: date, count: int) -> list[date]:
    from dateutil.relativedelta import relativedelta

    return [start + relativedelta(months=offset) for offset in range(count)]


def _observation(
    indicator_id: str,
    observation_date: date,
    value: str,
    *,
    frequency: str = "monthly",
    unit: str = "index",
    as_of: date | None = None,
    safe: bool | None = None,
    source: str = "fred",
    source_identity: str = "TEST",
    fallback: bool = False,
    health: RuntimeHealth = RuntimeHealth.HEALTHY,
    release_date: date | None | object = ...,
) -> EffectiveObservation:
    actual_release = observation_date if release_date is ... else cast(date | None, release_date)
    return EffectiveObservation(
        indicator_id=indicator_id,
        observation_date=observation_date,
        value=Decimal(value),
        unit=unit,
        frequency=frequency,
        source_used=source,
        source_identity=source_identity,
        source_policy_version="stage2b5-v3",
        registry_hash="7a13095907733a1d6092ef5231c67de7528886c7eda9eeb73ed2166764834758",
        mapping_version="mapping-v1",
        is_fallback=fallback,
        resolution_reason="TEST",
        mapping_quality=MappingQuality.VALID,
        provider_health=health,
        indicator_source_health=health,
        release_date=actual_release,
        retrieved_at=datetime(2026, 8, 26, tzinfo=UTC),
        vintage_as_of=as_of,
        vintage_source="ALFRED_VINTAGE" if as_of else None,
        is_point_in_time_safe=(as_of is not None if safe is None else safe),
    )


class FakeResolver:
    """Deterministic canonical-resolver substitute; never calls a provider."""

    def __init__(self, values: dict[date | None, list[EffectiveObservation]]) -> None:
        self.values = values
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        _session: Session,
        indicator_id: str,
        start: date,
        end: date,
        as_of: date | datetime | None,
        **kwargs: object,
    ) -> list[EffectiveObservation]:
        self.calls.append(
            {
                "indicator_id": indicator_id,
                "start": start,
                "end": end,
                "as_of": as_of,
                "kwargs": kwargs,
            }
        )
        key = as_of if isinstance(as_of, date) and not isinstance(as_of, datetime) else None
        return list(self.values[key])


def _cpi_series(
    *,
    as_of: date | None = None,
    safe: bool | None = None,
    final: str = "103",
) -> list[EffectiveObservation]:
    dates = _monthly_dates(date(2023, 4, 1), 13)
    values = [str(100 + Decimal(offset) / Decimal(4)) for offset in range(12)] + [final]
    return [
        _observation("US_CPI", item, value, as_of=as_of, safe=safe)
        for item, value in zip(dates, values, strict=True)
    ]


@pytest.mark.asyncio
async def test_current_feature_uses_canonical_inputs_and_complete_provenance() -> None:
    resolver = FakeResolver({None: _cpi_series()})
    engine = FeatureEngine(cast(Session, None), resolver=resolver)
    results = await engine.compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED", date(2024, 4, 1), date(2024, 4, 1)
    )
    assert len(results) == 1
    result = results[0]
    assert result.feature_status is FeatureStatus.OK
    assert result.value is not None
    assert result.input_count == result.expected_input_count == 4
    assert result.input_observation_dates == _monthly_dates(date(2024, 1, 1), 4)
    assert result.input_sources == ["fred"] * 4
    assert result.input_source_identities == ["TEST"] * 4
    assert result.input_mapping_versions == ["mapping-v1"] * 4
    assert result.source_policy_version == "stage2b5-v3"
    assert len(result.registry_hash) == 64
    assert len(result.calculation_hash) == 64
    assert result.as_of is None
    assert not result.is_point_in_time_safe


@pytest.mark.asyncio
async def test_arbitrary_weekend_pit_feature_is_safe_and_uses_same_formula() -> None:
    sunday = date(2024, 5, 5)
    current = _cpi_series()
    pit = _cpi_series(as_of=sunday)
    resolver = FakeResolver({None: current, sunday: pit})
    engine = FeatureEngine(cast(Session, None), resolver=resolver)
    current_result = await engine.compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED", date(2024, 4, 1), date(2024, 4, 1)
    )
    pit_result = await engine.compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED",
        date(2024, 4, 1),
        date(2024, 4, 1),
        sunday,
    )
    assert current_result[0].value == pit_result[0].value
    assert current_result[0].formula_id == pit_result[0].formula_id
    assert pit_result[0].as_of == sunday
    assert pit_result[0].is_point_in_time_safe
    assert pit_result[0].input_vintage_dates == [sunday] * 4


@pytest.mark.asyncio
async def test_pit_unsafe_input_fails_closed_without_a_value() -> None:
    as_of = date(2024, 5, 5)
    resolver = FakeResolver({as_of: _cpi_series(as_of=as_of, safe=False)})
    result = (
        await FeatureEngine(cast(Session, None), resolver=resolver).compute_feature(
            "US_CPI__INFLATION_3M_ANNUALIZED",
            date(2024, 4, 1),
            date(2024, 4, 1),
            as_of,
        )
    )[0]
    assert result.feature_status is FeatureStatus.PIT_UNSAFE
    assert result.value is None
    assert not result.is_point_in_time_safe


@pytest.mark.asyncio
async def test_future_observation_is_not_materialized_as_a_feature_target() -> None:
    as_of = date(2024, 5, 5)
    rows = _cpi_series(as_of=as_of)
    rows.append(_observation("US_CPI", date(2024, 6, 1), "999", as_of=as_of))
    resolver = FakeResolver({as_of: rows})
    results = await FeatureEngine(cast(Session, None), resolver=resolver).compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED",
        date(2024, 4, 1),
        date(2024, 5, 1),
        as_of,
    )
    assert results
    assert max(item.feature_observation_date for item in results) <= as_of
    assert all(item.feature_observation_date != date(2024, 6, 1) for item in results)


@pytest.mark.asyncio
async def test_revision_changes_input_hash_value_and_sets_revision_flag() -> None:
    first = _cpi_series(final="103")
    revised = _cpi_series(final="104")
    calls = 0

    async def resolver(*_args: object, **_kwargs: object) -> list[EffectiveObservation]:
        nonlocal calls
        calls += 1
        return first if calls == 1 else revised

    cache = FeatureResultCache()
    engine = FeatureEngine(cast(Session, None), resolver=resolver, cache=cache)
    before = await engine.compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED", date(2024, 4, 1), date(2024, 4, 1)
    )
    after = await engine.compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED", date(2024, 4, 1), date(2024, 4, 1)
    )
    assert before[0].value != after[0].value
    assert before[0].calculation_hash != after[0].calculation_hash
    assert QualityFlag.REVISION_CHANGED_RESULT in after[0].quality_flags
    assert len(cache) == 2


@pytest.mark.asyncio
async def test_current_and_pit_cache_entries_are_isolated() -> None:
    as_of = date(2024, 5, 5)
    cache = FeatureResultCache()
    resolver = FakeResolver({None: _cpi_series(), as_of: _cpi_series(as_of=as_of)})
    engine = FeatureEngine(cast(Session, None), resolver=resolver, cache=cache)
    await engine.compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED", date(2024, 4, 1), date(2024, 4, 1)
    )
    await engine.compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED",
        date(2024, 4, 1),
        date(2024, 4, 1),
        as_of,
    )
    assert len(cache) == 2


@pytest.mark.asyncio
async def test_repeat_computation_is_deterministic() -> None:
    resolver = FakeResolver({None: _cpi_series()})
    engine = FeatureEngine(cast(Session, None), resolver=resolver)
    first = await engine.compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED", date(2024, 4, 1), date(2024, 4, 1)
    )
    second = await engine.compute_feature(
        "US_CPI__INFLATION_3M_ANNUALIZED", date(2024, 4, 1), date(2024, 4, 1)
    )
    assert first == second


@pytest.mark.asyncio
async def test_indicator_batch_uses_one_resolver_call() -> None:
    resolver = FakeResolver({None: _cpi_series()})
    results = await FeatureEngine(
        cast(Session, None), resolver=resolver
    ).compute_indicator_features("US_CPI", date(2024, 4, 1), date(2024, 4, 1))
    assert len(resolver.calls) == 1
    assert {item.feature_id for item in results} == {
        "US_CPI__INFLATION_1M_ANNUALIZED",
        "US_CPI__INFLATION_3M_ANNUALIZED",
        "US_CPI__INFLATION_6M_ANNUALIZED",
        "US_CPI__INFLATION_YOY",
    }


@pytest.mark.asyncio
async def test_feature_set_groups_resolver_calls_by_indicator() -> None:
    cpi = _cpi_series()
    payroll = [
        _observation(
            "US_NONFARM_EMPLOYMENT",
            item,
            str(150000 + offset * 100),
            unit="thousands_persons",
        )
        for offset, item in enumerate(_monthly_dates(date(2024, 1, 1), 4))
    ]

    async def resolver(
        _session: Session, indicator_id: str, *_args: object, **_kwargs: object
    ) -> list[EffectiveObservation]:
        calls.append(indicator_id)
        return cpi if indicator_id == "US_CPI" else payroll

    calls: list[str] = []
    results = await FeatureEngine(cast(Session, None), resolver=resolver).compute_feature_set(
        [
            "US_CPI__INFLATION_3M_ANNUALIZED",
            "US_CPI__INFLATION_YOY",
            "US_NONFARM_EMPLOYMENT__CHANGE_1M",
        ],
        date(2024, 4, 1),
        date(2024, 4, 1),
    )
    assert calls == ["US_CPI", "US_NONFARM_EMPLOYMENT"]
    assert len(results) == 3


@pytest.mark.asyncio
async def test_quality_flags_preserve_fallback_health_release_and_mixed_sources() -> None:
    rows = _cpi_series()
    rows[-4] = _observation(
        "US_CPI",
        rows[-4].observation_date,
        str(rows[-4].value),
        fallback=True,
        source="fred",
        source_identity="CPIAUCSL",
        health=RuntimeHealth.STALE,
        release_date=None,
    )
    rows[-3] = _observation(
        "US_CPI",
        rows[-3].observation_date,
        str(rows[-3].value),
        source="bls",
        source_identity="CUSR0000SA0",
        health=RuntimeHealth.DEGRADED,
    )
    resolver = FakeResolver({None: rows})
    result = (
        await FeatureEngine(cast(Session, None), resolver=resolver).compute_feature(
            "US_CPI__INFLATION_3M_ANNUALIZED", date(2024, 4, 1), date(2024, 4, 1)
        )
    )[0]
    assert set(result.quality_flags) == {
        QualityFlag.FALLBACK_INPUT,
        QualityFlag.STALE_INPUT,
        QualityFlag.DEGRADED_INPUT,
        QualityFlag.RELEASE_DATE_UNAVAILABLE,
        QualityFlag.MIXED_INPUT_SOURCES,
    }


@pytest.mark.asyncio
async def test_missing_intermediate_period_returns_status_not_forward_fill() -> None:
    rows = _cpi_series()
    rows = [item for item in rows if item.observation_date != date(2024, 3, 1)]
    result = (
        await FeatureEngine(
            cast(Session, None), resolver=FakeResolver({None: rows})
        ).compute_feature("US_CPI__INFLATION_3M_ANNUALIZED", date(2024, 4, 1), date(2024, 4, 1))
    )[0]
    assert result.feature_status is FeatureStatus.MISSING_INPUT_PERIOD
    assert result.value is None
    assert result.input_count == 3
    assert result.expected_input_count == 4


@pytest.mark.asyncio
async def test_timestamp_pit_is_rejected_before_resolver() -> None:
    resolver = FakeResolver({None: _cpi_series()})
    with pytest.raises(PITGranularityUnsupported):
        await FeatureEngine(cast(Session, None), resolver=resolver).compute_feature(
            "US_CPI__INFLATION_3M_ANNUALIZED",
            date(2024, 4, 1),
            date(2024, 4, 1),
            datetime(2024, 5, 5, 12, tzinfo=UTC),
        )
    assert resolver.calls == []


@pytest.mark.asyncio
async def test_unknown_feature_and_indicator_fail_closed() -> None:
    engine = FeatureEngine(cast(Session, None), resolver=FakeResolver({None: []}))
    with pytest.raises(FeatureNotFound, match="UNKNOWN_FEATURE"):
        await engine.compute_feature("UNKNOWN", date(2024, 1, 1), date(2024, 1, 1))
    with pytest.raises(FeatureNotFound, match="UNKNOWN_INDICATOR"):
        await engine.compute_indicator_features("UNKNOWN", date(2024, 1, 1), date(2024, 1, 1))


def test_registry_hash_mismatch_fails_before_resolution() -> None:
    catalog = load_feature_catalog().model_copy(update={"required_source_registry_hash": "0" * 64})
    with pytest.raises(FeatureRegistryMismatch, match="REGISTRY_HASH_MISMATCH"):
        FeatureEngine(cast(Session, None), feature_catalog=catalog)


@pytest.mark.asyncio
async def test_source_integrity_error_is_not_converted_to_a_feature_value() -> None:
    async def resolver(*_args: object, **_kwargs: object) -> list[EffectiveObservation]:
        raise SourceIntegrityError("SOURCE_INTEGRITY_ERROR:source_value_conflict")

    with pytest.raises(SourceIntegrityError, match="source_value_conflict"):
        await FeatureEngine(cast(Session, None), resolver=resolver).compute_feature(
            "US_CPI__INFLATION_3M_ANNUALIZED", date(2024, 4, 1), date(2024, 4, 1)
        )


def test_lru_is_bounded_and_evicts_oldest_entry() -> None:
    cache = FeatureResultCache(max_entries=2)
    keys = [
        FeatureCacheKey(
            feature_id=f"FEATURE_{item}",
            feature_spec_version="stage3a-v1",
            formula_registry_version="stage3a-formulas-v1",
            indicator_id="US_CPI",
            start=date(2024, 1, 1),
            end=date(2024, 1, 1),
            as_of=None,
            source_policy_version="stage2b5-v3",
            registry_hash="a" * 64,
            resolver_input_hash=str(item) * 64,
        )
        for item in range(3)
    ]
    for key in keys:
        cache.put(key, [])
    assert len(cache) == 2
    assert cache.get(keys[0]) is None
    assert cache.get(keys[1]) == []


@pytest.mark.asyncio
async def test_result_serialization_contains_no_raw_payload() -> None:
    result = (
        await FeatureEngine(
            cast(Session, None), resolver=FakeResolver({None: _cpi_series()})
        ).compute_feature("US_CPI__INFLATION_3M_ANNUALIZED", date(2024, 4, 1), date(2024, 4, 1))
    )[0]
    payload = result.model_dump(mode="json")
    assert "raw_payload" not in payload
    assert payload["feature_spec_version"] == "stage3a-v1"
    assert payload["formula_registry_version"] == "stage3a-formulas-v1"


def test_feature_engine_uses_frozen_registry_without_mutating_it() -> None:
    before = load_source_registry()
    FeatureEngine(cast(Session, None), resolver=FakeResolver({None: []}))
    after = load_source_registry()
    assert before.registry_policy_version == after.registry_policy_version == "stage2b5-v3"
    assert before.registry_hash == after.registry_hash
