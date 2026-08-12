"""Offline full-SeriesKey and bounded ZIP tests for G.19."""

import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from app.data_sources.federal_reserve.exceptions import (
    FederalReserveIdentityError,
    FederalReserveResponseError,
)
from app.data_sources.federal_reserve.g19 import parse_g19_zip
from app.data_sources.federal_reserve.schemas import FederalReserveMappingIdentity


def _identity(**updates: object) -> FederalReserveMappingIdentity:
    values: dict[str, object] = {
        "release": "G19",
        "canonical_series_id": "DTCTL.M",
        "transport": "official_sdmx",
        "dataset": "G19",
        "group": "CCOUT",
        "representation": "level",
        "frequency": "M",
        "seasonal_adjustment": "SA",
        "source_unit": "billions_dollars",
        "data_path": "data/g19/g19_xml.zip",
        "first_available_period": "1943-01",
    }
    values.update(updates)
    return FederalReserveMappingIdentity.model_validate(values)


def _zip(xml: str, name: str = "g19.xml") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, xml)
    return buffer.getvalue()


def _xml(**updates: str) -> str:
    attrs = {
        "RELEASE": "G19",
        "GROUP": "CCOUT",
        "SERIES": "DTCTL.M",
        "REPRESENTATION": "level",
        "SEASONAL_ADJUSTMENT": "SA",
        "FREQ": "M",
        "UNIT": "billions_dollars",
        "MULTIPLIER": "9",
    }
    attrs.update(updates)
    rendered = " ".join(f'{key}="{value}"' for key, value in attrs.items())
    return f'<Data><Series {rendered}><Obs TIME_PERIOD="2025-01" OBS_VALUE="1.234567890123" /></Series></Data>'


def test_exact_series_key_and_decimal_normalization() -> None:
    result = parse_g19_zip(
        _zip(_xml()),
        [_identity()],
        max_members=3,
        max_uncompressed_bytes=10_000,
        release_date=date(2025, 2, 7),
    )["DTCTL.M"]

    assert result[0].value == Decimal("1234.567890123000")
    assert result[0].details["raw_value"] == "1.234567890123"
    assert result[0].details["canonical_value"] == "1234.567890123000"


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"SERIES": "DTCTL_FLOW"}, "unexpected_series_key"),
        ({"REPRESENTATION": "flow"}, "unexpected_series_key"),
        ({"SEASONAL_ADJUSTMENT": "NSA"}, "unexpected_series_key"),
        ({"FREQ": "A"}, "unexpected_series_key"),
        ({"MULTIPLIER": "6"}, "unexpected_series_key"),
    ],
)
def test_every_series_key_dimension_is_required(updates: dict[str, str], message: str) -> None:
    with pytest.raises(FederalReserveIdentityError, match=message):
        parse_g19_zip(
            _zip(_xml(**updates)),
            [_identity()],
            max_members=3,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )


def test_path_traversal_member_is_rejected() -> None:
    with pytest.raises(FederalReserveResponseError, match="unsafe member path"):
        parse_g19_zip(
            _zip(_xml(), "../g19.xml"),
            [_identity()],
            max_members=3,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )


def test_member_and_uncompressed_size_limits_are_enforced() -> None:
    with pytest.raises(FederalReserveResponseError, match="member limit"):
        parse_g19_zip(
            _zip(_xml()),
            [_identity()],
            max_members=0,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )
    with pytest.raises(FederalReserveResponseError, match="size limit"):
        parse_g19_zip(
            _zip(_xml()),
            [_identity()],
            max_members=3,
            max_uncompressed_bytes=5,
            release_date=None,
        )


def test_malformed_xml_is_rejected() -> None:
    with pytest.raises(FederalReserveResponseError, match="malformed"):
        parse_g19_zip(
            _zip("<Data>"),
            [_identity()],
            max_members=3,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )


def test_one_release_independently_extracts_both_credit_series() -> None:
    first = _xml()
    second = _xml(SERIES="DTCTLR.M").removeprefix("<Data>").removesuffix("</Data>")
    combined = first.removesuffix("</Data>") + second + "</Data>"
    identities = [_identity(), _identity(canonical_series_id="DTCTLR.M")]

    result = parse_g19_zip(
        _zip(combined),
        identities,
        max_members=3,
        max_uncompressed_bytes=10_000,
        release_date=None,
    )

    assert set(result) == {"DTCTL.M", "DTCTLR.M"}
    assert all(len(items) == 1 for items in result.values())


def test_zip_with_ambiguous_multiple_xml_members_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("one.xml", _xml())
        archive.writestr("two.xml", _xml())

    with pytest.raises(FederalReserveResponseError, match="exactly one XML"):
        parse_g19_zip(
            buffer.getvalue(),
            [_identity()],
            max_members=3,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )
