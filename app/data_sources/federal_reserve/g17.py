"""Strict parser for Federal Reserve Board G.17 static text releases."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data_sources.federal_reserve.exceptions import (
    FederalReserveBaseYearError,
    FederalReserveIdentityError,
    FederalReserveResponseError,
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
    required = {identity.canonical_series_id, identity.transport_series_code or ""}
    if not all(token and token in documentation_text for token in required):
        raise FederalReserveIdentityError("unexpected_series_identity")
    base_year: int | None = None
    if identity.representation == "index":
        base_year = parse_g17_base_year(metadata_text)
        if base_year != identity.expected_base_year:
            raise FederalReserveBaseYearError(
                f"unexpected_base_year: expected {identity.expected_base_year}, returned {base_year}"
            )
    release_match = _RELEASE_DATE.search(metadata_text)
    release_date = (
        date(*(int(release_match.group(index)) for index in (1, 2, 3))) if release_match else None
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
    for line in text.splitlines():
        fields = line.replace(",", " ").split()
        if len(fields) < 13 or not re.fullmatch(r"(?:19|20)\d{2}", fields[0]):
            continue
        year = int(fields[0])
        for month, raw_value in enumerate(fields[1:13], start=1):
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
