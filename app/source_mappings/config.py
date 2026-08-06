"""Load reviewed official-source mappings from YAML."""

from pathlib import Path

import yaml

from app.source_mappings.schemas import SourceMappingCatalog, SourceMappingDefinition

DEFAULT_BLS_MAPPING_PATH = Path("config/data_sources/bls.yaml")


def load_source_mapping_catalog(path: Path = DEFAULT_BLS_MAPPING_PATH) -> SourceMappingCatalog:
    """Load one source mapping catalog without guessing or substituting Series IDs."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("mappings"), list):
        raise ValueError("Source mapping catalog requires a mappings list")
    source = str(raw["source"])
    mappings = [SourceMappingDefinition.model_validate(item) for item in raw["mappings"]]
    return SourceMappingCatalog(
        schema_version=str(raw["schema_version"]),
        source=source,
        mappings=mappings,
        source_path=path,
    )
