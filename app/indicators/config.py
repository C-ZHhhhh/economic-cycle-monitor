"""Load and validate the versioned indicator YAML catalog."""

from pathlib import Path
from typing import Any

import yaml

from app.indicators.schemas import IndicatorCatalog, IndicatorDefinition

DEFAULT_CATALOG_PATH = Path("config/indicators/us_macro.yaml")


def load_indicator_catalog(path: Path = DEFAULT_CATALOG_PATH) -> IndicatorCatalog:
    """Load the indicator catalog and merge explicit versioned defaults."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Indicator catalog must be a YAML mapping")
    defaults = raw.get("defaults")
    items = raw.get("indicators")
    if not isinstance(defaults, dict) or not isinstance(items, list):
        raise ValueError("Indicator catalog requires defaults and indicators")
    definitions: list[IndicatorDefinition] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each indicator must be a YAML mapping")
        merged: dict[str, Any] = {**defaults, **item}
        definitions.append(IndicatorDefinition.model_validate(merged))
    return IndicatorCatalog(
        schema_version=str(raw["schema_version"]),
        agent=str(raw["agent"]),
        country=str(raw["country"]),
        stage=str(raw["stage"]),
        indicators=definitions,
        source_path=path,
    )
