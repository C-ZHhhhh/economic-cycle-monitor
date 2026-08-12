"""Strict identity and operational-semantics tests for DOL Weekly Claims."""

from datetime import date
from decimal import Decimal

import pytest

from app.data_sources.dol.exceptions import DolIdentityError, DolResponseError
from app.data_sources.dol.schemas import DolMappingIdentity
from app.data_sources.dol.weekly_claims import parse_weekly_claims


def _identity(**updates: object) -> DolMappingIdentity:
    values: dict[str, object] = {
        "report": "r539cy",
        "scope": "us",
        "program": "regular_state",
        "series_concept": "initial_claims",
        "seasonal_adjustment": "SA",
        "frequency": "W",
        "unit": "persons",
        "row_path": ".//row",
        "scope_path": "scope",
        "program_path": "program",
        "concept_path": "concept",
        "seasonal_path": "seasonal",
        "frequency_path": "frequency",
        "unit_path": "unit",
        "observation_path": "week_ending",
        "value_path": "value",
        "release_date_path": "release_date",
        "expected_wire_values": {
            "scope": ["United States"],
            "program": ["Regular State Program"],
            "concept": ["Initial Claims"],
            "seasonal": ["Seasonally Adjusted"],
            "frequency": ["Weekly"],
            "unit": ["Persons"],
        },
        "first_available_period": "1967-01-07",
    }
    values.update(updates)
    return DolMappingIdentity.model_validate(values)


def _row(concept: str, week: str, value: str, release: str = "2026-08-06") -> str:
    return (
        "<row><scope>United States</scope><program>Regular State Program</program>"
        f"<concept>{concept}</concept><seasonal>Seasonally Adjusted</seasonal>"
        "<frequency>Weekly</frequency><unit>Persons</unit>"
        f"<week_ending>{week}</week_ending><value>{value}</value>"
        f"<release_date>{release}</release_date></row>"
    )


def test_week_ending_not_release_date_is_observation_date() -> None:
    item = parse_weekly_claims(
        f"<report>{_row('Initial Claims', '2026-08-01', '250000')}</report>".encode(),
        _identity(),
    )[0]

    assert item.date == date(2026, 8, 1)
    assert item.release_date == date(2026, 8, 6)
    assert item.value == Decimal("250000")


def test_pending_paths_block_candidate_without_guessing_tags() -> None:
    with pytest.raises(DolIdentityError, match="pending_xml_identity"):
        parse_weekly_claims(b"<report />", _identity(row_path=None))


def test_wrong_program_or_nsa_is_an_identity_error() -> None:
    xml = _row("Initial Claims", "2026-08-01", "250000").replace(
        "Regular State Program", "All Programs"
    )
    with pytest.raises(DolIdentityError, match="no exact DOL identity"):
        parse_weekly_claims(f"<report>{xml}</report>".encode(), _identity())


def test_initial_and_insured_unemployment_keep_independent_week_endings() -> None:
    initial = parse_weekly_claims(
        f"<report>{_row('Initial Claims', '2026-08-01', '250000')}</report>".encode(),
        _identity(),
    )[0]
    continued_identity = _identity(
        series_concept="insured_unemployment",
        expected_wire_values={
            **_identity().expected_wire_values,
            "concept": ["Insured Unemployment"],
        },
    )
    continued = parse_weekly_claims(
        f"<report>{_row('Insured Unemployment', '2026-07-25', '1900000')}</report>".encode(),
        continued_identity,
    )[0]

    assert initial.date == date(2026, 8, 1)
    assert continued.date == date(2026, 7, 25)


def test_historical_row_without_release_date_preserves_null_semantics() -> None:
    xml = _row("Initial Claims", "2026-08-01", "250000").replace(
        "<release_date>2026-08-06</release_date>", ""
    )
    item = parse_weekly_claims(
        f"<report>{xml}</report>".encode(),
        _identity(release_date_path=None),
    )[0]

    assert item.release_date is None
    assert item.release_date_status == "not_provided"


@pytest.mark.parametrize(
    ("week", "value"),
    [("not-a-date", "250000"), ("2026-08-01", "not-a-number")],
)
def test_invalid_week_or_value_is_rejected(week: str, value: str) -> None:
    with pytest.raises(DolResponseError, match="observation is invalid"):
        parse_weekly_claims(
            f"<report>{_row('Initial Claims', week, value)}</report>".encode(),
            _identity(),
        )
