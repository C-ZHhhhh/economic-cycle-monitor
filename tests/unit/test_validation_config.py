"""Stage 3F validation authority and phase-separation tests."""

from pathlib import Path

import pytest
import yaml

from app.validation.config import load_validation_catalog
from app.validation.schemas import (
    PilotSummary,
    StopReason,
    UniverseMode,
    ValidationCadence,
    ValidationPhase,
    ValidationStatus,
)


def _mutated(tmp_path: Path, mutation: object) -> Path:
    raw = yaml.safe_load(Path("config/validation/us_macro_stage3f.yaml").read_text())
    assert isinstance(mutation, tuple)
    keys, value = mutation
    target = raw
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    path = tmp_path / "stage3f.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_stage3f_authority_loads_and_binds_all_production_layers() -> None:
    catalog = load_validation_catalog()
    assert catalog.validation_spec_version == "stage3f-v1"
    assert catalog.validation_phase is ValidationPhase.PILOT
    assert catalog.authorities.source_registry_version == "stage2b5-v3"
    assert catalog.authorities.feature_spec_version == "stage3a-v1"
    assert catalog.authorities.normalization_spec_version == "stage3b-v1"
    assert catalog.authorities.pillar_spec_version == "stage3c-v1"
    assert catalog.authorities.composite_spec_version == "stage3d-v1"
    assert catalog.authorities.regime_spec_version == "stage3e-v1"
    assert len(catalog.authorities.ordered_hashes()) == 6


def test_stage3f_universes_and_cadences_are_separate() -> None:
    catalog = load_validation_catalog()
    assert catalog.universe_modes == list(UniverseMode)
    assert catalog.cadence.primary is ValidationCadence.RELEASE_EVENT_EOD
    assert catalog.cadence.benchmark is ValidationCadence.MONTH_END_EOD
    assert not catalog.cadence.intraday_reconstruction


def test_stage3f_pilot_and_provisional_full_budgets_are_separate() -> None:
    catalog = load_validation_catalog()
    assert catalog.budgets.pilot.external_alfred_http_requests == 2000
    assert catalog.budgets.provisional_full_run.external_alfred_http_requests == 25000
    assert not catalog.budgets.provisional_full_run.approved


@pytest.mark.parametrize(
    "mutation,match",
    [
        ((["validation_spec_version"], "stage3f-v2"), "VALIDATION_SPEC_VERSION"),
        ((["required_regime_spec_hash"], "wrong"), "PRODUCTION_AUTHORITY_DRIFT"),
        ((["validation_phase"], "FULL"), "FULL_HISTORICAL_RUN_NOT_APPROVED"),
        ((["budgets", "pilot", "external_alfred_http_requests"], 2001), "CAP_DRIFT"),
        ((["budgets", "provisional_full_run", "approved"], True), "NOT_APPROVED"),
        ((["cadence", "intraday_reconstruction"], True), "INTRADAY"),
        ((["artifact_formats", "parquet_enabled"], True), "PARQUET"),
        ((["materialization", "sensitivity_refetch_allowed"], True), "REFETCH"),
        ((["hysteresis_research", "production_writeback"], True), "WRITEBACK"),
        (
            (["revision_sensitivity", "build_alternate_latest_revised_history"], True),
            "ALTERNATE_REVISED_HISTORY",
        ),
    ],
)
def test_validation_config_fails_closed(
    tmp_path: Path, mutation: tuple[list[str], object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        load_validation_catalog(_mutated(tmp_path, mutation))


def test_validation_authority_does_not_shadow_production_configs() -> None:
    raw = yaml.safe_load(Path("config/validation/us_macro_stage3f.yaml").read_text())
    prohibited = {"feature_formulas", "normalization", "pillars", "regimes", "weights"}
    assert prohibited.isdisjoint(raw)


def test_pilot_cannot_report_full_pass() -> None:
    with pytest.raises(ValueError, match="PILOT_CANNOT_REPORT_FULL_PASS"):
        PilotSummary(
            validation_phase=ValidationPhase.PILOT,
            validation_status=ValidationStatus.PASS,
            stop_reason=StopReason.PLANNER_COMPLETE,
            pilot_window_start="2026-01-01",
            pilot_window_end="2026-01-02",
            release_event_evaluations=0,
            month_end_evaluations=0,
            same_day_releases_coalesced=0,
            counters={
                "logical_pit_units": 0,
                "completed_logical_units": 0,
                "external_alfred_http_requests": 0,
                "retry_count": 0,
                "cache_reuse_count": 0,
                "incremental_storage_bytes": 0,
                "elapsed_wall_seconds": 0,
            },
            future_leakage_count=0,
            same_as_of_violation_count=0,
            pit_unsafe_classified_count=0,
            deterministic_replay_match=True,
            authority_drift_count=0,
            history_run_hashes={},
            boundaries=[],
            unresolved_boundaries=[],
            artifact_hashes={},
        )
