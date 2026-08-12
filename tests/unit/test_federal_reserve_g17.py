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
        "first_available_period": "1919-01",
    }
    values.update(updates)
    return FederalReserveMappingIdentity.model_validate(values)


def test_current_base_and_both_g17_identities_are_required() -> None:
    base, released = validate_g17_identity(
        _identity(),
        "IP.B50001.S Total industrial production transport B50001",
        "Index 2017=100; released 2026-08-01",
    )

    assert base == 2017
    assert released == date(2026, 8, 1)


def test_unexpected_2022_base_blocks_writes() -> None:
    with pytest.raises(FederalReserveBaseYearError, match="unexpected_base_year"):
        validate_g17_identity(
            _identity(),
            "IP.B50001.S Total industrial production B50001",
            "Index 2022=100",
        )


def test_transport_short_code_cannot_replace_canonical_identity() -> None:
    with pytest.raises(FederalReserveIdentityError, match="unexpected_series_identity"):
        validate_g17_identity(_identity(), "B50001 Total industrial production", "2017=100")


def test_capacity_series_cannot_match_utilization_target() -> None:
    utilization = _identity(
        canonical_series_id="CAPUTL.GMF.S",
        transport_series_code="GMF",
        representation="utilization",
        source_unit="percent",
        expected_base_year=None,
    )
    with pytest.raises(FederalReserveIdentityError, match="unexpected_series_identity"):
        validate_g17_identity(utilization, "CAP.GMF.S Manufacturing capacity GMF", "release")


def test_text_parser_preserves_decimal_missing_and_release_date() -> None:
    body = b"2025 100.00000000000001 . 102 103 104 105 106 107 108 109 110 111\n"
    observations = parse_g17_text(body, _identity(), base_year=2017, release_date=date(2026, 1, 15))

    assert len(observations) == 11
    assert observations[0].value == Decimal("100.00000000000001")
    assert observations[0].details["raw_value"] == "100.00000000000001"
    assert observations[0].release_date == date(2026, 1, 15)


def test_absent_release_date_remains_explicitly_not_provided() -> None:
    body = b"2025 1 2 3 4 5 6 7 8 9 10 11 12\n"
    observation = parse_g17_text(body, _identity(), base_year=2017, release_date=None)[0]

    assert observation.release_date is None
    assert observation.release_date_status == "not_provided"


def test_non_utf8_g17_payload_is_rejected() -> None:
    with pytest.raises(FederalReserveResponseError, match="not UTF-8"):
        parse_g17_text(b"\xff\xfe", _identity(), base_year=2017, release_date=None)


def test_non_numeric_g17_value_is_rejected() -> None:
    with pytest.raises(FederalReserveResponseError, match="non-numeric"):
        parse_g17_text(
            b"2025 1 2 3 4 bad 6 7 8 9 10 11 12\n",
            _identity(),
            base_year=2017,
            release_date=None,
        )


def test_g17_payload_without_monthly_rows_is_rejected() -> None:
    with pytest.raises(FederalReserveResponseError, match="no monthly observations"):
        parse_g17_text(b"header only\n", _identity(), base_year=2017, release_date=None)
