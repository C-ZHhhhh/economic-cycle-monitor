"""Deterministic semantic and evaluation identities for DLCR-003."""

from datetime import date
from decimal import Decimal

from app.data_sources.fred.schemas import FredObservation
from app.source_registry.information_sets import evaluation_result_hash, information_set_hash
from app.source_registry.materialization import load_pit_materialization_catalog


def _observation(day: int, value: str | None) -> FredObservation:
    return FredObservation(
        realtime_start=date(2026, 1, 10),
        realtime_end=date(2026, 1, 10),
        date=date(2025, day, 1),
        value=Decimal(value) if value is not None else None,
    )


def _source_hash(observations: list[FredObservation]) -> str:
    return information_set_hash(
        indicator_id="US_TEST",
        provider_series_id="TEST",
        source_information_set_date=date(2026, 1, 10),
        unit="percent",
        frequency="monthly",
        source_registry_hash="r" * 64,
        materialization=load_pit_materialization_catalog(),
        observations=observations,
    )


def test_information_hash_ignores_http_order_and_decimal_scale() -> None:
    first = [_observation(1, "100.0"), _observation(2, None)]
    second = [_observation(2, None), _observation(1, "100.000")]
    assert _source_hash(first) == _source_hash(second)


def test_information_hash_changes_for_economic_value() -> None:
    assert _source_hash([_observation(1, "100")]) != _source_hash([_observation(1, "101")])


def test_evaluation_hash_changes_without_changing_source_hash() -> None:
    materialization = load_pit_materialization_catalog()
    source_hash = _source_hash([_observation(1, "100")])
    first = evaluation_result_hash(
        evaluation_as_of=date(2026, 1, 10),
        information_hash=source_hash,
        start=date(2025, 1, 1),
        end=date(2025, 1, 1),
        materialization=materialization,
    )
    second = evaluation_result_hash(
        evaluation_as_of=date(2026, 1, 11),
        information_hash=source_hash,
        start=date(2025, 1, 1),
        end=date(2025, 1, 1),
        materialization=materialization,
    )
    assert first != second
