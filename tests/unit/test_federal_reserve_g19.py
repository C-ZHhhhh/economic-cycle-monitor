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
        "source_unit": "millions_usd",
        "data_member": "G19_data.xml",
        "wire_frequency": "129",
        "data_representation": "MILLDOLL",
        "wire_unit": "Currency",
        "unit_multiplier": "1000000",
        "holder": "ALL",
        "credit_type": "TOTAL",
        "currency": "USD",
        "data_path": "releases/g19/data/FRB_g19_xml.zip",
        "documentation_path": "releases/g19/current/default.htm",
        "description_keywords": ["consumer", "credit"],
        "first_available_period": "1943-01",
    }
    values.update(updates)
    return FederalReserveMappingIdentity.model_validate(values)


def _series(**updates: str) -> str:
    attrs = {
        "CREDTYP": "TOTAL",
        "CURRENCY": "USD",
        "DATAREP": "MILLDOLL",
        "FREQ": "129",
        "HOLDER": "ALL",
        "SA": "SA",
        "SERIES_NAME": "DTCTL.M",
        "UNIT": "Currency",
        "UNIT_MULT": "1000000",
    }
    attrs.update(updates)
    rendered = " ".join(f'{key}="{value}"' for key, value in attrs.items())
    return (
        f"<Series {rendered}>"
        '<Obs TIME_PERIOD="2025-01-31" OBS_VALUE="1234.567890123" OBS_STATUS="A" />'
        "</Series>"
    )


def _xml(*series: str, dataset: str = "CCOUT") -> str:
    return f'<MessageGroup><DataSet id="{dataset}">{"".join(series)}</DataSet></MessageGroup>'


def _zip(
    xml: str,
    *,
    data_member: str = "G19_data.xml",
    include_structure: bool = True,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(data_member, xml)
        if include_structure:
            archive.writestr("G19_struct.xml", "<Structure />")
            archive.writestr("G19_CCOUT.xsd", "<schema />")
    return buffer.getvalue()


def test_real_series_key_preserves_decimal_and_uses_identity_normalization() -> None:
    result = parse_g19_zip(
        _zip(_xml(_series())),
        [_identity()],
        max_members=5,
        max_uncompressed_bytes=10_000,
        release_date=date(2025, 2, 7),
    )["DTCTL.M"]

    assert result[0].value == Decimal("1234.567890123")
    assert result[0].value != Decimal("1234567.890123")
    assert result[0].details["raw_value"] == "1234.567890123"
    assert result[0].details["canonical_value"] == "1234.567890123"
    assert result[0].details["data_representation"] == "MILLDOLL"
    assert result[0].details["unit_multiplier"] == "1000000"
    assert result[0].details["raw_frequency"] == "129"
    assert result[0].details["frequency"] == "M"
    assert result[0].details["canonical_unit"] == "millions_usd"
    assert result[0].details["normalization_factor"] == "1"
    assert result[0].details["source_identity"]["unit_multiplier"] == "1000000"
    assert len(result[0].details["source_identity_hash"]) == 64


@pytest.mark.parametrize(
    "updates",
    [
        {"SERIES_NAME": "DTCTL_FLOW"},
        {"DATAREP": "PCTCHG"},
        {"SA": "NSA"},
        {"FREQ": "130"},
        {"UNIT_MULT": "1000"},
        {"HOLDER": "FINCO"},
        {"CREDTYP": "REV"},
        {"CURRENCY": "EUR"},
        {"UNIT": "Percent"},
    ],
)
def test_every_total_credit_series_key_dimension_is_required(updates: dict[str, str]) -> None:
    with pytest.raises(FederalReserveIdentityError, match="unexpected_series_key"):
        parse_g19_zip(
            _zip(_xml(_series(**updates))),
            [_identity()],
            max_members=5,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )


def test_revolving_credit_requires_rev_dimension_independently() -> None:
    revolving = _identity(
        canonical_series_id="DTCTLR.M",
        credit_type="REV",
        description_keywords=["revolving", "consumer", "credit"],
    )
    with pytest.raises(FederalReserveIdentityError, match="unexpected_series_key"):
        parse_g19_zip(
            _zip(_xml(_series(SERIES_NAME="DTCTLR.M", CREDTYP="TOTAL"))),
            [revolving],
            max_members=5,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )


def test_unknown_numeric_frequency_is_not_treated_as_monthly() -> None:
    with pytest.raises(FederalReserveIdentityError, match="unexpected_series_key"):
        parse_g19_zip(
            _zip(_xml(_series(FREQ="999"))),
            [_identity()],
            max_members=5,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )


def test_one_release_independently_extracts_both_credit_series() -> None:
    identities = [
        _identity(),
        _identity(canonical_series_id="DTCTLR.M", credit_type="REV"),
    ]
    body = _zip(
        _xml(
            _series(),
            _series(SERIES_NAME="DTCTLR.M", CREDTYP="REV"),
        )
    )

    result = parse_g19_zip(
        body,
        identities,
        max_members=5,
        max_uncompressed_bytes=10_000,
        release_date=None,
    )

    assert set(result) == {"DTCTL.M", "DTCTLR.M"}
    assert all(len(items) == 1 for items in result.values())


def test_exact_dataset_identity_is_required() -> None:
    with pytest.raises(FederalReserveIdentityError, match="unexpected_release_dataset_identity"):
        parse_g19_zip(
            _zip(_xml(_series(), dataset="WRONG")),
            [_identity()],
            max_members=5,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )


def test_real_archive_shape_selects_data_xml_among_structure_files() -> None:
    result = parse_g19_zip(
        _zip(_xml(_series())),
        [_identity()],
        max_members=5,
        max_uncompressed_bytes=10_000,
        release_date=None,
    )

    assert len(result["DTCTL.M"]) == 1


def test_full_release_selects_ccout_and_ignores_other_datasets() -> None:
    body = _zip(
        '<MessageGroup><DataSet id="TERMS">'
        f'{_series()}</DataSet><DataSet id="CCOUT">{_series()}</DataSet></MessageGroup>'
    )

    result = parse_g19_zip(
        body,
        [_identity()],
        max_members=5,
        max_uncompressed_bytes=10_000,
        release_date=None,
    )

    assert len(result["DTCTL.M"]) == 1


def test_missing_exact_data_member_is_rejected() -> None:
    with pytest.raises(FederalReserveResponseError, match="exact XML data member"):
        parse_g19_zip(
            _zip(_xml(_series()), data_member="other.xml"),
            [_identity()],
            max_members=5,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )


def test_path_traversal_member_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("G19_data.xml", _xml(_series()))
        archive.writestr("../G19_struct.xml", "<Structure />")
    with pytest.raises(FederalReserveResponseError, match="unsafe member path"):
        parse_g19_zip(
            buffer.getvalue(),
            [_identity()],
            max_members=5,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )


def test_member_and_uncompressed_size_limits_are_enforced() -> None:
    body = _zip(_xml(_series()))
    with pytest.raises(FederalReserveResponseError, match="member limit"):
        parse_g19_zip(
            body,
            [_identity()],
            max_members=2,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )
    with pytest.raises(FederalReserveResponseError, match="size limit"):
        parse_g19_zip(
            body,
            [_identity()],
            max_members=5,
            max_uncompressed_bytes=5,
            release_date=None,
        )


def test_malformed_data_xml_is_rejected() -> None:
    with pytest.raises(FederalReserveResponseError, match="malformed"):
        parse_g19_zip(
            _zip("<MessageGroup>"),
            [_identity()],
            max_members=5,
            max_uncompressed_bytes=10_000,
            release_date=None,
        )
