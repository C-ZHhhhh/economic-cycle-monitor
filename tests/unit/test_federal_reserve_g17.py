"""Offline identity, base-year, date, and precision tests for G.17."""

from datetime import date
from decimal import Decimal

import pytest

from app.data_sources.federal_reserve.exceptions import (
    FederalReserveBaseYearError,
    FederalReserveIdentityError,
    FederalReserveResponseError,
)
from app.data_sources.federal_reserve.g17 import (
    parse_g17_text,
    validate_g17_identity,
)
from app.data_sources.federal_reserve.schemas import FederalReserveMappingIdentity


def _identity(**updates: object) -> FederalReserveMappingIdentity:
    values: dict[str, object] = {
        "release": "G17",
        "canonical_series_id": "IP.B50001.S",
        "transport": "official_text",
        "transport_series_code": "B50001",
        "dataset": "G17",
        "representation": "index",
        "frequency": "M",
        "seasonal_adjustment": "SA",
        "source_unit": "index",
        "expected_base_year": 2017,
        "data_path": "releases/g17/ipdisk/ip_sa.txt",
        "documentation_path": "releases/g17/ipdisk/ip_series.txt",
        "description_keywords": ["total", "index"],
        "first_available_period": "1919-01",
    }
    values.update(updates)
    return FederalReserveMappingIdentity.model_validate(values)


def test_current_base_and_both_g17_identities_are_required() -> None:
    base, released = validate_g17_identity(
        _identity(),
        "Total index  B50001",
        "Index 2017=100; Release Date: July 17, 2026",
    )

    assert base == 2017
    assert released == date(2026, 7, 17)


def test_unexpected_2022_base_blocks_writes() -> None:
    with pytest.raises(FederalReserveBaseYearError, match="unexpected_base_year"):
        validate_g17_identity(
            _identity(),
            "Total index B50001",
            "Index 2022=100",
        )


def test_transport_short_code_cannot_replace_canonical_identity() -> None:
    with pytest.raises(FederalReserveIdentityError, match="unexpected_series_identity"):
        validate_g17_identity(
            _identity(canonical_series_id="IP.WRONG.S"), "Total index B50001", "2017=100"
        )


def test_capacity_series_cannot_match_utilization_target() -> None:
    utilization = _identity(
        canonical_series_id="CAP.GMF.S",
        transport_series_code="GMF",
        representation="utilization",
        source_unit="percent",
        expected_base_year=None,
        description_keywords=["manufacturing", "naics"],
    )
    with pytest.raises(FederalReserveIdentityError, match="unexpected_series_identity"):
        validate_g17_identity(utilization, "Manufacturing (NAICS) GMF", "release")


def test_text_parser_preserves_decimal_missing_and_release_date() -> None:
    body = b'"B50001" 2025 100.00000000000001 . 102 103 104 105 106 107 108 109 110 111\n'
    observations = parse_g17_text(body, _identity(), base_year=2017, release_date=date(2026, 1, 15))

    assert len(observations) == 11
    assert observations[0].value == Decimal("100.00000000000001")
    assert observations[0].details["raw_value"] == "100.00000000000001"
    assert observations[0].release_date == date(2026, 1, 15)


def test_absent_release_date_remains_explicitly_not_provided() -> None:
    body = b'"B50001" 2025 1 2 3 4 5 6 7 8 9 10 11 12\n'
    observation = parse_g17_text(body, _identity(), base_year=2017, release_date=None)[0]

    assert observation.release_date is None
    assert observation.release_date_status == "not_provided"


def test_non_utf8_g17_payload_is_rejected() -> None:
    with pytest.raises(FederalReserveResponseError, match="not UTF-8"):
        parse_g17_text(b"\xff\xfe", _identity(), base_year=2017, release_date=None)


def test_non_numeric_g17_value_is_rejected() -> None:
    with pytest.raises(FederalReserveResponseError, match="non-numeric"):
        parse_g17_text(
            b'"B50001" 2025 1 2 3 4 bad 6 7 8 9 10 11 12\n',
            _identity(),
            base_year=2017,
            release_date=None,
        )


def test_g17_payload_without_monthly_rows_is_rejected() -> None:
    with pytest.raises(FederalReserveResponseError, match="no monthly observations"):
        parse_g17_text(b"header only\n", _identity(), base_year=2017, release_date=None)


def test_real_g17_multi_series_shape_selects_only_exact_transport_code() -> None:
    body = (
        b'"B50001: Total index"\n'
        b'"B50001" 2025 1 2 3 4 5 6 7 8 9 10 11 12\n'
        b'"B50030" 2025 91 92 93 94 95 96 97 98 99 100 101 102\n'
    )

    observations = parse_g17_text(body, _identity(), base_year=2017, release_date=date(2026, 7, 17))

    assert len(observations) == 12
    assert observations[0].value == Decimal("1")
    assert observations[-1].value == Decimal("12")


def test_current_partial_year_preserves_every_published_month() -> None:
    body = b'"B50001" 2026 101.0 102.0 103.0 104.0 105.0 106.0\n'

    observations = parse_g17_text(body, _identity(), base_year=2017, release_date=date(2026, 7, 17))

    assert len(observations) == 6
    assert observations[-1].source_period == "2026-06"
    assert observations[-1].value == Decimal("106.0")
