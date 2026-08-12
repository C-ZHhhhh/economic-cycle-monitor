"""Strict configured-path parser for DOL National Weekly Claims XML."""

from datetime import date
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from app.data_sources.dol.exceptions import DolIdentityError, DolResponseError
from app.data_sources.dol.schemas import DolMappingIdentity
from app.data_sources.official.schemas import OfficialObservation

_PATH_FIELDS = (
    "row_path",
    "scope_path",
    "program_path",
    "concept_path",
    "seasonal_path",
    "frequency_path",
    "unit_path",
    "observation_path",
    "value_path",
)


def parse_weekly_claims(body: bytes, identity: DolMappingIdentity) -> list[OfficialObservation]:
    """Parse only a Live-reviewed XML shape and exact configured wire identity."""
    missing = [field for field in _PATH_FIELDS if getattr(identity, field) is None]
    if missing or not identity.expected_wire_values:
        raise DolIdentityError(f"pending_xml_identity: missing {missing}")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise DolResponseError("DOL Weekly Claims XML is malformed") from error
    rows = root.findall(identity.row_path or "")
    if not rows:
        raise DolIdentityError("unexpected_xml_schema: configured row path returned no rows")
    observations: list[OfficialObservation] = []
    for row in rows:
        values = {
            "scope": _required_text(row, identity.scope_path),
            "program": _required_text(row, identity.program_path),
            "concept": _required_text(row, identity.concept_path),
            "seasonal": _required_text(row, identity.seasonal_path),
            "frequency": _required_text(row, identity.frequency_path),
            "unit": _required_text(row, identity.unit_path),
        }
        if not all(
            values[name].casefold() in {value.casefold() for value in allowed}
            for name, allowed in identity.expected_wire_values.items()
        ):
            continue
        period = _required_text(row, identity.observation_path)
        raw_value = _required_text(row, identity.value_path)
        try:
            observation_date = date.fromisoformat(period)
            value = Decimal(raw_value.replace(",", ""))
        except (ValueError, InvalidOperation) as error:
            raise DolResponseError("DOL Weekly Claims observation is invalid") from error
        release_text = (
            row.findtext(identity.release_date_path)
            if identity.release_date_path is not None
            else None
        )
        try:
            release_date = date.fromisoformat(release_text.strip()) if release_text else None
        except ValueError as error:
            raise DolResponseError("DOL release date is invalid") from error
        observations.append(
            OfficialObservation(
                date=observation_date,
                value=value,
                source_period=period,
                release_date=release_date,
                release_date_status=("provided" if release_date else "not_provided"),
                details={
                    "report": identity.report,
                    "scope": values["scope"],
                    "program": values["program"],
                    "concept": values["concept"],
                    "seasonal_adjustment": values["seasonal"],
                    "frequency": values["frequency"],
                    "raw_value": raw_value,
                    "raw_unit": values["unit"],
                    "canonical_value": str(value),
                },
            )
        )
    if not observations:
        raise DolIdentityError("unexpected_series_identity: no exact DOL identity matched")
    return sorted(observations, key=lambda item: item.date)


def _required_text(row: ElementTree.Element, path: str | None) -> str:
    value = row.findtext(path or "")
    if value is None or not value.strip():
        raise DolIdentityError(f"unexpected_xml_schema: no value at {path}")
    return value.strip()
