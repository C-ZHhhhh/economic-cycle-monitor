"""Bounded streaming parser for Federal Reserve Board G.19 SDMX ZIP releases."""

import io
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import IO, Any
from xml.etree import ElementTree

from app.data_sources.federal_reserve.exceptions import (
    FederalReserveIdentityError,
    FederalReserveResponseError,
)
from app.data_sources.federal_reserve.identity import (
    federal_reserve_source_identity,
    federal_reserve_source_identity_hash,
)
from app.data_sources.federal_reserve.schemas import FederalReserveMappingIdentity
from app.data_sources.official.schemas import OfficialObservation

_MISSING = {"", ".", "--", "NA", "N/A"}


def open_bounded_xml_member(
    body: bytes,
    *,
    data_member: str,
    max_members: int,
    max_uncompressed_bytes: int,
) -> tuple[zipfile.ZipFile, IO[bytes]]:
    """Validate a release ZIP and return the exact configured data member."""
    if not body.startswith(b"PK\x03\x04"):
        raise FederalReserveResponseError("G.19 release is not a ZIP archive")
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
        members = archive.infolist()
    except zipfile.BadZipFile as error:
        raise FederalReserveResponseError("G.19 release ZIP is truncated or malformed") from error
    if not members or len(members) > max_members:
        archive.close()
        raise FederalReserveResponseError("G.19 ZIP member limit exceeded")
    if sum(item.file_size for item in members) > max_uncompressed_bytes:
        archive.close()
        raise FederalReserveResponseError("G.19 ZIP uncompressed size limit exceeded")
    for item in members:
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts:
            archive.close()
            raise FederalReserveResponseError("G.19 ZIP contains an unsafe member path")
    data_members = [item for item in members if item.filename == data_member]
    if len(data_members) != 1 or not data_member.lower().endswith(".xml"):
        archive.close()
        raise FederalReserveResponseError("G.19 ZIP does not contain the exact XML data member")
    return archive, archive.open(data_members[0])


def parse_g19_zip(
    body: bytes,
    identities: list[FederalReserveMappingIdentity],
    *,
    max_members: int,
    max_uncompressed_bytes: int,
    release_date: date | None,
) -> dict[str, list[OfficialObservation]]:
    """Parse one full G.19 release and independently match complete SeriesKeys."""
    if not identities:
        return {}
    data_members = {item.data_member for item in identities}
    groups = {item.group for item in identities}
    if len(data_members) != 1 or None in data_members or len(groups) != 1 or None in groups:
        raise FederalReserveIdentityError("unexpected_release_dataset_identity")
    data_member = next(iter(data_members))
    expected_group = next(iter(groups))
    assert data_member is not None
    assert expected_group is not None
    archive, stream = open_bounded_xml_member(
        body,
        data_member=data_member,
        max_members=max_members,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )
    expected = {item.canonical_series_id: item for item in identities}
    results: dict[str, list[OfficialObservation]] = {key: [] for key in expected}
    matched: set[str] = set()
    returned_groups: set[str] = set()
    active_group: str | None = None
    try:
        try:
            iterator = ElementTree.iterparse(stream, events=("start", "end"))
            for event, element in iterator:
                local = _local(element.tag)
                if event == "start" and local == "DataSet":
                    dataset_id = element.attrib.get("id") or element.attrib.get("ID")
                    if dataset_id:
                        active_group = dataset_id
                        returned_groups.add(dataset_id)
                    continue
                if event != "end":
                    continue
                if local == "DataSet":
                    active_group = None
                    element.clear()
                    continue
                if local != "Series":
                    continue
                if active_group != expected_group:
                    element.clear()
                    continue
                key = _series_key(element)
                for canonical, identity in expected.items():
                    if not _matches_identity(key, identity):
                        continue
                    matched.add(canonical)
                    results[canonical].extend(_observations(element, identity, key, release_date))
                element.clear()
        except ElementTree.ParseError as error:
            raise FederalReserveResponseError("G.19 release XML is malformed") from error
    finally:
        stream.close()
        archive.close()
    missing = sorted(set(expected) - matched)
    if expected_group not in returned_groups:
        raise FederalReserveIdentityError(
            f"unexpected_release_dataset_identity: returned {sorted(returned_groups)}"
        )
    if missing:
        raise FederalReserveIdentityError(f"unexpected_series_key: missing {missing}")
    return {key: sorted(value, key=lambda item: item.date) for key, value in results.items()}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _series_key(element: ElementTree.Element) -> dict[str, str]:
    key = {str(name).upper(): value for name, value in element.attrib.items()}
    for descendant in element.iter():
        if _local(descendant.tag) == "Value":
            concept = descendant.attrib.get("concept") or descendant.attrib.get("id")
            value = descendant.attrib.get("value")
            if concept and value:
                key[concept.upper()] = value
    return key


def _matches_identity(key: dict[str, str], identity: FederalReserveMappingIdentity) -> bool:
    """Match all nine dimensions in the real compact G.19 SeriesKey."""
    expected = {
        "CREDTYP": identity.credit_type,
        "CURRENCY": identity.currency,
        "DATAREP": identity.data_representation,
        "FREQ": identity.wire_frequency,
        "HOLDER": identity.holder,
        "SA": identity.seasonal_adjustment,
        "SERIES_NAME": identity.canonical_series_id,
        "UNIT": identity.wire_unit,
        "UNIT_MULT": identity.unit_multiplier,
    }
    return key == expected


def _observations(
    series: ElementTree.Element,
    identity: FederalReserveMappingIdentity,
    key: dict[str, str],
    release_date: date | None,
) -> list[OfficialObservation]:
    observations: list[OfficialObservation] = []
    source_identity = federal_reserve_source_identity(identity)
    source_identity_hash = federal_reserve_source_identity_hash(source_identity)
    for element in series.iter():
        if _local(element.tag) != "Obs":
            continue
        period = element.attrib.get("TIME_PERIOD") or element.attrib.get("time")
        raw_value = element.attrib.get("OBS_VALUE") or element.attrib.get("value")
        if not period or raw_value is None or raw_value.upper() in _MISSING:
            continue
        try:
            value = Decimal(raw_value)
            observation_date = date.fromisoformat(period[:7] + "-01")
        except (InvalidOperation, ValueError) as error:
            raise FederalReserveResponseError("G.19 observation is invalid") from error
        details: dict[str, Any] = {
            "release": "G19",
            "canonical_series_id": identity.canonical_series_id,
            "series_key": key,
            "frequency": identity.frequency,
            "raw_frequency": identity.wire_frequency,
            "seasonal_adjustment": identity.seasonal_adjustment,
            "representation": identity.representation,
            "raw_value": raw_value,
            "raw_unit": identity.source_unit,
            "wire_unit": identity.wire_unit,
            "data_representation": identity.data_representation,
            "unit_multiplier": identity.unit_multiplier,
            "normalization_factor": "1",
            "canonical_unit": "millions_usd",
            "canonical_value": str(value),
            "source_identity": source_identity,
            "source_identity_hash": source_identity_hash,
        }
        observations.append(
            OfficialObservation(
                date=observation_date,
                value=value,
                source_period=period,
                release_date=release_date,
                release_date_status=("provided" if release_date else "not_provided"),
                details=details,
            )
        )
    return observations
