"""Permanent-gap authority tests: binding, evidence, and point-in-time knowledge."""

from datetime import date
from pathlib import Path

import pytest
import yaml

from app.source_registry.config import load_source_registry
from app.source_registry.permanent_gaps import (
    DEFAULT_PERMANENT_GAP_PATH,
    load_permanent_gap_catalog,
)

OCTOBER_2025 = date(2025, 10, 1)


def _write(tmp_path: Path, mutate: object) -> Path:
    raw = yaml.safe_load(DEFAULT_PERMANENT_GAP_PATH.read_text(encoding="utf-8"))
    assert callable(mutate)
    mutate(raw)
    path = tmp_path / "permanent_gaps.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def test_authority_is_bound_to_the_active_registry_and_hashed() -> None:
    registry = load_source_registry()
    catalog = load_permanent_gap_catalog()
    assert catalog.required_source_registry_hash == registry.registry_hash
    assert len(catalog.permanent_gap_hash) == 64
    assert catalog.permanent_gap_hash == load_permanent_gap_catalog().permanent_gap_hash


def test_october_2025_lapse_covers_household_survey_cpi_and_deflated_retail() -> None:
    catalog = load_permanent_gap_catalog()
    for indicator_id in (
        "US_UNEMPLOYMENT_RATE",
        "US_U6_RATE",
        "US_PARTICIPATION_RATE",
        "US_EMPLOYMENT_POP_RATIO",
        "US_CPI",
        "US_CORE_CPI",
        "US_REAL_RETAIL_SALES",
    ):
        assert catalog.periods_for(indicator_id) == frozenset({OCTOBER_2025})
    assert catalog.periods_for("US_NONFARM_EMPLOYMENT") == frozenset()


def test_point_in_time_evaluation_only_sees_gaps_already_confirmed() -> None:
    catalog = load_permanent_gap_catalog()
    assert catalog.periods_for("US_UNEMPLOYMENT_RATE", as_of=date(2025, 12, 15)) == frozenset()
    assert catalog.periods_for("US_UNEMPLOYMENT_RATE", as_of=date(2025, 12, 16)) == frozenset(
        {OCTOBER_2025}
    )


def test_every_gap_carries_reviewable_official_evidence() -> None:
    for gap in load_permanent_gap_catalog().gaps:
        assert gap.official_source_url.startswith("https://www.bls.gov/")
        assert gap.reason.strip()
        assert gap.known_since >= gap.observation_period


def test_registry_hash_drift_fails_closed(tmp_path: Path) -> None:
    path = _write(tmp_path, lambda raw: raw.update(required_source_registry_hash="0" * 64))
    with pytest.raises(ValueError, match="hash mismatch"):
        load_permanent_gap_catalog(path)


def test_unknown_indicator_and_duplicate_declaration_are_rejected(tmp_path: Path) -> None:
    unknown = _write(tmp_path, lambda raw: raw["gaps"][0]["indicator_ids"].append("US_NOT_REAL"))
    with pytest.raises(ValueError, match="unknown indicator"):
        load_permanent_gap_catalog(unknown)

    def duplicate(raw: dict[str, list[dict[str, list[str]]]]) -> None:
        raw["gaps"][1]["indicator_ids"].append("US_UNEMPLOYMENT_RATE")

    with pytest.raises(ValueError, match="twice"):
        load_permanent_gap_catalog(_write(tmp_path, duplicate))


def test_non_https_evidence_and_mid_month_period_are_rejected(tmp_path: Path) -> None:
    def insecure(raw: dict[str, list[dict[str, str]]]) -> None:
        raw["gaps"][0]["official_source_url"] = "http://www.bls.gov/notice"

    with pytest.raises(ValueError, match="https"):
        load_permanent_gap_catalog(_write(tmp_path, insecure))

    def mid_month(raw: dict[str, list[dict[str, date]]]) -> None:
        raw["gaps"][0]["observation_period"] = date(2025, 10, 15)

    with pytest.raises(ValueError, match="native period start"):
        load_permanent_gap_catalog(_write(tmp_path, mid_month))
