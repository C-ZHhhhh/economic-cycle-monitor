"""Strict parser for Federal Reserve Board G.17 static text releases."""

import re
from calendar import month_name
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data_sources.federal_reserve.exceptions import (
    FederalReserveBaseYearError,
    FederalReserveIdentityError,
    FederalReserveResponseError,
)
from app.data_sources.federal_reserve.identity import (
    federal_reserve_source_identity,
    federal_reserve_source_identity_hash,
)
from app.data_sources.federal_reserve.schemas import FederalReserveMappingIdentity
from app.data_sources.official.schemas import OfficialObservation

_BASE_PATTERNS = (
    re.compile(r"(?:index\s*)?(20\d{2})\s*=\s*100", re.IGNORECASE),
    re.compile(r"base(?:\s+year)?\D{0,20}(20\d{2})", re.IGNORECASE),
)
_RELEASE_DATE = re.compile(
    r"(?:release(?:d|\s+date)?|updated)\D{0,20}"
    r"(20\d{2})[-/]([01]?\d)[-/]([0-3]?\d)",
    re.IGNORECASE,
)
_NAMED_RELEASE_DATE = re.compile(
    r"release\s+date\s*:\s*([A-Za-z]+)\s+([0-3]?\d),\s*(20\d{2})",
    re.IGNORECASE,
)
_MISSING = {"", ".", "--", "NA", "N/A"}


def parse_g17_base_year(metadata_text: str) -> int:
    """Read the current index base from official release metadata."""
    for pattern in _BASE_PATTERNS:
        match = pattern.search(metadata_text)
        if match:
            return int(match.group(1))
    raise FederalReserveResponseError("G.17 metadata does not expose an index base year")


def validate_g17_identity(
    identity: FederalReserveMappingIdentity,
    documentation_text: str,
    metadata_text: str,
) -> tuple[int | None, date | None]:
    """Validate canonical/transport identities and the configured dynamic base gate."""
    if identity.release != "G17" or identity.transport != "official_text":
        raise FederalReserveIdentityError("unexpected_release_transport")
    transport = identity.transport_series_code or ""
    prefix = "IP" if identity.representation == "index" else "CAPUTL"
    if identity.canonical_series_id != f"{prefix}.{transport}.S":
        raise FederalReserveIdentityError("unexpected_series_identity")
    matching_lines = [
        line
        for line in documentation_text.splitlines()
        if re.search(rf"\b{re.escape(transport)}\b", line)
    ]
    if len(matching_lines) != 1:
        raise FederalReserveIdentityError("unexpected_transport_series_code")
    normalized_line = matching_lines[0].casefold()
    if not all(keyword.casefold() in normalized_line for keyword in identity.description_keywords):
        raise FederalReserveIdentityError("unexpected_series_description")
    base_year: int | None = None
    if identity.representation == "index":
        base_year = parse_g17_base_year(metadata_text)
        if base_year != identity.expected_base_year:
            raise FederalReserveBaseYearError(
                f"unexpected_base_year: expected {identity.expected_base_year}, returned {base_year}"
            )
    release_match = _RELEASE_DATE.search(metadata_text)
    release_date: date | None
    if release_match:
        release_date = date(*(int(release_match.group(index)) for index in (1, 2, 3)))
    else:
        named = _NAMED_RELEASE_DATE.search(metadata_text)
        months = {name.casefold(): number for number, name in enumerate(month_name) if name}
        release_date = (
            date(int(named.group(3)), months[named.group(1).casefold()], int(named.group(2)))
            if named and named.group(1).casefold() in months
            else None
        )
    return base_year, release_date


def parse_g17_text(
    body: bytes,
    identity: FederalReserveMappingIdentity,
    *,
    base_year: int | None,
    release_date: date | None,
) -> list[OfficialObservation]:
    """Parse a Board monthly text snapshot without float conversion."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise FederalReserveResponseError("G.17 text is not UTF-8") from error
    observations: list[OfficialObservation] = []
    source_identity = federal_reserve_source_identity(identity)
    source_identity_hash = federal_reserve_source_identity_hash(source_identity)
    for line in text.splitlines():
        fields = line.replace(",", " ").replace('"', "").split()
        if (
            len(fields) < 3
            or fields[0] != identity.transport_series_code
            or not re.fullmatch(r"(?:19|20)\d{2}", fields[1])
        ):
            continue
        year = int(fields[1])
        for month, raw_value in enumerate(fields[2:14], start=1):
            if raw_value.upper() in _MISSING:
                continue
            try:
                value = Decimal(raw_value)
            except InvalidOperation as error:
                raise FederalReserveResponseError("G.17 contains a non-numeric value") from error
            details: dict[str, Any] = {
                "release": "G17",
                "canonical_series_id": identity.canonical_series_id,
                "transport_series_code": identity.transport_series_code,
                "representation": identity.representation,
                "frequency": identity.frequency,
                "seasonal_adjustment": identity.seasonal_adjustment,
                "raw_value": raw_value,
                "raw_unit": identity.source_unit,
                "base_year": base_year,
                "canonical_value": str(value),
                "source_identity": source_identity,
                "source_identity_hash": source_identity_hash,
            }
            observations.append(
                OfficialObservation(
                    date=date(year, month, 1),
                    value=value,
                    source_period=f"{year}-{month:02d}",
                    release_date=release_date,
                    release_date_status=("provided" if release_date else "not_provided"),
                    details=details,
                )
            )
    if not observations:
        raise FederalReserveResponseError("G.17 text contains no monthly observations")
    return observations
