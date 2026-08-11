"""Canonical Census source-identity helpers."""

import hashlib
import json
from typing import Any

from app.data_sources.census.schemas import CensusMappingIdentity, CensusRow


def census_source_identity(
    identity: CensusMappingIdentity, row: CensusRow | None = None
) -> dict[str, Any]:
    """Build a transparent source identity from configured and returned dimensions."""
    result: dict[str, Any] = {
        "dataset": identity.dataset,
        "program": identity.program,
        "category_code": identity.category_code,
        "data_type_code": identity.data_type_code,
        "seasonally_adj": row.seasonally_adj if row is not None else identity.seasonally_adj,
        "geo_level_code": row.geo_level_code if row is not None else identity.geo_level_code,
        "geography": identity.geography,
    }
    if row is not None:
        returned = row.returned_dimensions()
        for field in identity.identity_fields:
            result[field] = returned.get(field)
    return result


def census_source_identity_hash(identity: dict[str, Any]) -> str:
    """Hash but never replace the canonical official identity dimensions."""
    material = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode()).hexdigest()
