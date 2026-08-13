"""Strict configured-path parser for DOL National Weekly Claims XML."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from app.data_sources.dol.exceptions import DolIdentityError, DolResponseError
from app.data_sources.dol.schemas import DolMappingIdentity
from app.data_sources.official.schemas import OfficialObservation

_PATH_FIELDS = (
    "root_tag",
    "row_path",
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
    allowed_roots = identity.expected_wire_values.get("root_tag", [])
    if root.tag != identity.root_tag or root.tag.casefold() not in {
        value.casefold() for value in allowed_roots
    }:
        raise DolIdentityError("unexpected_report_identity: DOL XML root tag did not match")
    rows = root.findall(identity.row_path or "")
    if not rows:
        raise DolIdentityError("unexpected_xml_schema: configured row path returned no rows")
    report_run_date = _report_run_date(root, identity)
    observations: list[OfficialObservation] = []
    for row in rows:
        period = _required_text(row, identity.observation_path)
        raw_value = row.findtext(identity.value_path or "")
        normalized_raw = raw_value.strip() if raw_value is not None else ""
        if normalized_raw in identity.missing_value_markers:
            continue
        try:
            observation_date = datetime.strptime(period, identity.observation_date_format).date()
            value = Decimal(normalized_raw.replace(",", ""))
        except (ValueError, InvalidOperation) as error:
            raise DolResponseError("DOL Weekly Claims observation is invalid") from error
        observations.append(
            OfficialObservation(
                date=observation_date,
                value=value,
                source_period=period,
                release_date=None,
                release_date_status="not_provided",
                details={
                    "report": identity.report,
                    "root_tag": root.tag,
                    "scope": identity.scope,
                    "program": identity.program,
                    "concept": identity.series_concept,
                    "seasonal_adjustment": identity.seasonal_adjustment,
                    "frequency": identity.frequency,
                    "raw_value": normalized_raw,
                    "raw_unit": identity.unit,
                    "canonical_value": str(value),
                    "observation_path": identity.observation_path,
                    "value_path": identity.value_path,
                    "report_run_date": (report_run_date.isoformat() if report_run_date else None),
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


def _report_run_date(root: ElementTree.Element, identity: DolMappingIdentity) -> date | None:
    """Read the report-generation date without treating it as a release date."""
    if identity.report_run_date_attribute is None:
        return None
    raw_value = root.attrib.get(identity.report_run_date_attribute)
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return datetime.strptime(raw_value.strip(), identity.report_run_date_format).date()
    except ValueError as error:
        raise DolResponseError("DOL report run date is invalid") from error
