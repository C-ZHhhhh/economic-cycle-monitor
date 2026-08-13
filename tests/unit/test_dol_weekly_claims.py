"""Strict identity and operational-semantics tests for DOL Weekly Claims."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.data_sources.dol.exceptions import DolIdentityError, DolResponseError
from app.data_sources.dol.schemas import DolMappingIdentity
from app.data_sources.dol.weekly_claims import parse_weekly_claims

FIXTURE = Path("tests/fixtures/dol_r539cy_national.xml")


def _identity(**updates: object) -> DolMappingIdentity:
    values: dict[str, object] = {
        "report": "r539cy",
        "scope": "us",
        "program": "regular_state",
        "series_concept": "initial_claims",
        "seasonal_adjustment": "SA",
        "frequency": "W",
        "unit": "persons",
        "root_tag": "r539cyNational",
        "row_path": "./week",
        "observation_path": "weekEnded",
        "value_path": "InitialClaims/SA",
        "observation_date_format": "%m/%d/%Y",
        "report_run_date_attribute": "rundate",
        "report_run_date_format": "%m/%d/%Y",
        "missing_value_markers": [""],
        "expected_wire_values": {"root_tag": ["r539cyNational"]},
        "first_available_period": "1967-01-07",
    }
    values.update(updates)
    return DolMappingIdentity.model_validate(values)


def test_real_shape_uses_week_ended_and_skips_empty_future_values() -> None:
    items = parse_weekly_claims(FIXTURE.read_bytes(), _identity())

    assert [item.date for item in items] == [date(2026, 7, 25), date(2026, 8, 1)]
    assert [item.value for item in items] == [Decimal("211000"), Decimal("207000")]
    assert items[-1].release_date is None
    assert items[-1].release_date_status == "not_provided"
    assert items[-1].details["report_run_date"] == "2026-08-13"
    assert items[-1].details["value_path"] == "InitialClaims/SA"


def test_pending_paths_block_candidate_without_guessing_tags() -> None:
    with pytest.raises(DolIdentityError, match="pending_xml_identity"):
        parse_weekly_claims(b"<r539cyNational />", _identity(row_path=None))


def test_wrong_report_root_is_an_identity_error() -> None:
    xml = FIXTURE.read_bytes().replace(b"r539cyNational", b"r539cyState")
    with pytest.raises(DolIdentityError, match="unexpected_report_identity"):
        parse_weekly_claims(xml, _identity())


def test_nsa_and_insured_unemployment_rate_are_not_selected() -> None:
    items = parse_weekly_claims(FIXTURE.read_bytes(), _identity())

    assert items[0].value == Decimal("211000")
    assert items[0].details["value_path"] == "InitialClaims/SA"
    assert items[0].value != Decimal("184321")
    assert items[0].value != Decimal("1.2")


def test_initial_and_continued_claims_use_independently_configured_value_paths() -> None:
    initial = parse_weekly_claims(FIXTURE.read_bytes(), _identity())[0]
    continued = parse_weekly_claims(
        FIXTURE.read_bytes(),
        _identity(series_concept="insured_unemployment", value_path="ContinuedClaims/SA"),
    )[0]

    assert initial.value == Decimal("211000")
    assert continued.value == Decimal("1841000")
    assert initial.date == continued.date == date(2026, 7, 25)
    assert initial.details["concept"] == "initial_claims"
    assert continued.details["concept"] == "insured_unemployment"
    assert initial.details["value_path"] != continued.details["value_path"]


def test_invalid_week_or_value_is_rejected() -> None:
    invalid_week = FIXTURE.read_bytes().replace(b"07/25/2026", b"not-a-date")
    with pytest.raises(DolResponseError, match="observation is invalid"):
        parse_weekly_claims(invalid_week, _identity())

    invalid_value = FIXTURE.read_bytes().replace(b"211,000", b"not-a-number")
    with pytest.raises(DolResponseError, match="observation is invalid"):
        parse_weekly_claims(invalid_value, _identity())


def test_invalid_report_run_date_is_rejected_without_becoming_release_date() -> None:
    xml = FIXTURE.read_bytes().replace(b"8/13/2026", b"not-a-date")
    with pytest.raises(DolResponseError, match="report run date is invalid"):
        parse_weekly_claims(xml, _identity())
