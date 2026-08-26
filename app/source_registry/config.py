"""Load the unique source-resolution policy authority from YAML."""

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.source_registry.schemas import SourceRegistryCatalog, SourceRegistryPolicy

DEFAULT_SOURCE_REGISTRY_PATH = Path("config/data_sources/source_registry.yaml")


def load_source_registry(
    path: Path = DEFAULT_SOURCE_REGISTRY_PATH,
) -> SourceRegistryCatalog:
    """Load and hash the complete versioned source registry."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("policies"), list):
        raise ValueError("Source registry requires a policies list")
    raw_policies: list[Any] = raw["policies"]
    policies = [SourceRegistryPolicy.model_validate(item) for item in raw_policies]
    hash_payload = {
        "schema_version": str(raw["schema_version"]),
        "registry_policy_version": str(raw["registry_policy_version"]),
        "policies": [item.model_dump(mode="json") for item in policies],
    }
    registry_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SourceRegistryCatalog(
        schema_version=str(raw["schema_version"]),
        registry_policy_version=str(raw["registry_policy_version"]),
        policies=policies,
        source_path=path,
        registry_hash=registry_hash,
    )


def storage_source_name(source: str) -> str:
    """Map a logical registry source to the existing source-separated DB name."""
    return {
        "FRED_CURRENT": "fred",
        "ALFRED_VINTAGE": "fred",
        "BLS": "bls",
        "BEA": "bea",
        "CENSUS": "census",
        "FEDERAL_RESERVE": "federal_reserve",
        "DOL": "dol",
    }[source]
