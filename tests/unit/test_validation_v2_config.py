"""Stage 3F-v2 is a bounded, offline DLCR-003 authority."""

from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from app.validation.config import load_validation_catalog
from app.validation.equivalence import _economic_payload
from app.validation.v2 import load_stage3f_v2_catalog


class _EconomicComparisonFixture(BaseModel):
    calculation_hash: str
    input_retrieved_at: list[str]
    economic_status: str


def test_v1_is_preserved_and_v2_binds_new_pit_authorities() -> None:
    v1 = load_validation_catalog()
    v2 = load_stage3f_v2_catalog()
    assert v1.validation_spec_version == "stage3f-v1"
    assert v2.validation_spec_version == "stage3f-v2"
    assert v2.required_pit_materialization_spec_version == "pit-materialization-v2"
    assert v2.required_calculation_provenance_version == "pit-calculation-provenance-v2"
    assert v2.equivalence.required_snapshot_pairs == 594
    assert v2.equivalence.external_alfred_http_requests == 0
    assert not v2.second_pilot_approved
    assert not v2.full_historical_run_approved


def test_v2_fails_closed_on_pit_authority_drift(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config/validation/us_macro_stage3f_v2.yaml").read_text())
    raw["required_pit_materialization_spec_hash"] = "wrong"
    path = tmp_path / "v2.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="PIT_AUTHORITY_DRIFT"):
        load_stage3f_v2_catalog(path)


def test_economic_equivalence_excludes_only_physical_retrieval_provenance() -> None:
    fixture = _EconomicComparisonFixture(
        calculation_hash="a" * 64,
        input_retrieved_at=["2026-08-31T00:00:00Z"],
        economic_status="OK",
    )
    assert _economic_payload(fixture) == {"economic_status": "OK"}
