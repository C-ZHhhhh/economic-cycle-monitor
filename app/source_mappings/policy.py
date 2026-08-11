"""Source-neutral mapping-role policy helpers."""

from collections.abc import Mapping
from typing import Any


def source_mapping_type(parameters: Mapping[str, Any]) -> str:
    """Return the configured role, defaulting legacy BLS/BEA mappings to DIRECT."""
    return str(parameters.get("mapping_type", "DIRECT")).upper()


def source_mapping_is_direct(parameters: Mapping[str, Any]) -> bool:
    """Return whether a mapping may write target observations and become primary."""
    return source_mapping_type(parameters) == "DIRECT"
