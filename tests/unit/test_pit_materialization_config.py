"""DLCR-003 materialization-authority contract tests."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from app.source_registry.config import load_source_registry
from app.source_registry.materialization import load_pit_materialization_catalog


def test_materialization_authority_is_bound_and_deterministic() -> None:
    registry = load_source_registry()
    first = load_pit_materialization_catalog(source_registry=registry)
    second = load_pit_materialization_catalog(source_registry=registry)

    assert first.pit_materialization_spec_version == "pit-materialization-v2"
    assert first.calculation_provenance.version == "pit-calculation-provenance-v2"
    assert first.specification_hash == second.specification_hash
    assert first.calculation_provenance_hash == second.calculation_provenance_hash
    assert first.required_source_registry_version == "stage2b5-v3"
    assert first.required_source_registry_hash == registry.registry_hash
    assert first.bulk_observations.enabled is False
    assert first.concurrency.alfred_materialization == 1


def test_open_utc_eod_is_not_complete() -> None:
    catalog = load_pit_materialization_catalog()
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    assert catalog.closed_eod.latest_closed_date(now).isoformat() == "2026-08-30"


def test_registry_drift_fails_closed(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config/data_sources/pit_materialization.yaml").read_text())
    raw["required_source_registry_hash"] = "0" * 64
    path = tmp_path / "pit.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="PIT_MATERIALIZATION_SOURCE_REGISTRY_DRIFT"):
        load_pit_materialization_catalog(path, source_registry=load_source_registry())
