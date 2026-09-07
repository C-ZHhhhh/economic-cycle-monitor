"""DLCR-004 revision-provenance authority contract tests."""

from pathlib import Path

import pytest
import yaml

from app.source_registry.config import load_source_registry
from app.source_registry.materialization import load_pit_materialization_catalog
from app.source_registry.revision_provenance import load_revision_provenance_catalog


def test_revision_provenance_authority_is_deterministic_and_bound() -> None:
    registry = load_source_registry()
    materialization = load_pit_materialization_catalog(source_registry=registry)
    first = load_revision_provenance_catalog(
        source_registry=registry, materialization=materialization
    )
    second = load_revision_provenance_catalog(
        source_registry=registry, materialization=materialization
    )

    assert first.revision_provenance_version == "revision-provenance-v2"
    assert first.specification_hash == second.specification_hash
    assert first.required_source_registry_hash == registry.registry_hash
    assert first.required_pit_materialization_hash == materialization.specification_hash
    assert first.predecessor.prohibit_runtime_inference is True
    assert first.comparison.new_information_is_revision is False
    assert first.current_path.runtime_diagnostic_enabled is False


def test_revision_provenance_upstream_drift_fails_closed(tmp_path: Path) -> None:
    registry = load_source_registry()
    materialization = load_pit_materialization_catalog(source_registry=registry)
    raw = yaml.safe_load(Path("config/data_sources/revision_provenance.yaml").read_text())
    raw["required_pit_materialization_hash"] = "0" * 64
    path = tmp_path / "revision.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="REVISION_PROVENANCE_PIT_MATERIALIZATION_DRIFT"):
        load_revision_provenance_catalog(
            path, source_registry=registry, materialization=materialization
        )


def test_stage3_revision_authority_has_no_process_local_predecessor_memory() -> None:
    prohibited = {
        "_revision_values",
        "_revision_distributions",
        "_revision_results",
        "_remember_revision",
    }
    source = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "app/features/service.py",
            "app/signals/service.py",
            "app/pillars/service.py",
            "app/composites/service.py",
            "app/regimes/service.py",
        )
    )
    assert all(item not in source for item in prohibited)
