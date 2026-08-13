"""Canonical Federal Reserve Board source-identity helpers."""

import hashlib
import json
from typing import Any

from app.data_sources.federal_reserve.schemas import FederalReserveMappingIdentity


def federal_reserve_source_identity(
    identity: FederalReserveMappingIdentity,
) -> dict[str, Any]:
    """Build a transparent identity from every configured Board dimension."""
    return {
        "release": identity.release,
        "canonical_series_id": identity.canonical_series_id,
        "transport": identity.transport,
        "transport_series_code": identity.transport_series_code,
        "dataset": identity.dataset,
        "group": identity.group,
        "representation": identity.representation,
        "frequency": identity.frequency,
        "seasonal_adjustment": identity.seasonal_adjustment,
        "source_unit": identity.source_unit,
    }


def federal_reserve_source_identity_hash(identity: dict[str, Any]) -> str:
    """Hash but never replace the canonical Board identity dimensions."""
    material = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode()).hexdigest()
