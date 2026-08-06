"""Load and validate the versioned indicator YAML catalog."""

from pathlib import Path
from typing import Any

import yaml

from app.indicators.schemas import (
    IndicatorCatalog,
    IndicatorDefinition,
    MetadataValidationConfig,
)

DEFAULT_CATALOG_PATH = Path("config/indicators/us_macro.yaml")


def load_indicator_catalog(path: Path = DEFAULT_CATALOG_PATH) -> IndicatorCatalog:
    """Load the indicator catalog and merge explicit versioned defaults."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Indicator catalog must be a YAML mapping")
    defaults = raw.get("defaults")
    items = raw.get("indicators")
    metadata_validation_raw = raw.get("metadata_validation")
    if (
        not isinstance(defaults, dict)
        or not isinstance(items, list)
        or not isinstance(metadata_validation_raw, dict)
    ):
        raise ValueError("Indicator catalog requires metadata_validation, defaults, and indicators")
    metadata_validation = MetadataValidationConfig.model_validate(metadata_validation_raw)
    definitions: list[IndicatorDefinition] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each indicator must be a YAML mapping")
        merged: dict[str, Any] = {**defaults, **item}
        expected_unit = str(merged["expected_unit"])
        try:
            merged["unit_aliases"] = metadata_validation.unit_aliases[expected_unit]
        except KeyError as error:
            raise ValueError(
                f"No metadata unit aliases configured for {expected_unit!r}"
            ) from error
        definitions.append(IndicatorDefinition.model_validate(merged))
    return IndicatorCatalog(
        schema_version=str(raw["schema_version"]),
        agent=str(raw["agent"]),
        country=str(raw["country"]),
        stage=str(raw["stage"]),
        metadata_validation=metadata_validation,
        indicators=definitions,
        source_path=path,
    )
