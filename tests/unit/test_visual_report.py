"""Visual report tests: wording authority, history condensation, and page rendering."""

import json
from pathlib import Path

import pytest
import yaml

from agents.macro.visual_report import (
    DEFAULT_REPORT_GLOSSARY_PATH,
    HISTORY_BASELINE_VERSION,
    _signed,
    build_history_baseline,
    load_report_glossary,
    render_visual_report,
)
from app.composites.config import load_composite_catalog
from app.source_registry.config import load_source_registry


def _line(day: str, growth: str | None, inflation: str | None) -> str:
    def condition(value: str | None, low: str, mid: str, high: str) -> str | None:
        if value is None:
            return None
        number = float(value)
        return high if number > 0.1 else low if number < -0.1 else mid

    return json.dumps(
        {
            "regime": {
                "evaluation_as_of": day,
                "growth_strength": growth,
                "inflation_pressure": inflation,
                "growth_condition": condition(growth, "WEAK", "NEUTRAL", "STRONG"),
                "inflation_condition": condition(
                    inflation, "LOW_PRESSURE", "NEUTRAL_PRESSURE", "HIGH_PRESSURE"
                ),
            }
        }
    )


def test_glossary_covers_every_indicator_pillar_and_regime() -> None:
    glossary = load_report_glossary()
    registry = load_source_registry()
    composite = load_composite_catalog()
    assert set(registry.by_indicator()) <= set(glossary.indicators)
    assert {item.pillar_id for item in composite.growth.pillars} <= set(glossary.pillars)
    assert composite.inflation_pillar_id in glossary.pillars
    assert set(composite.context_pillar_ids) <= set(glossary.pillars)
    assert len(glossary.regimes) == 9
    assert {item.indicator_id for item in glossary.small_multiples} <= set(glossary.indicators)


def test_glossary_missing_an_indicator_is_rejected(tmp_path: Path) -> None:
    raw = yaml.safe_load(DEFAULT_REPORT_GLOSSARY_PATH.read_text(encoding="utf-8"))
    del raw["indicators"]["US_CPI"]
    path = tmp_path / "glossary.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="US_CPI"):
        load_report_glossary(path)


def test_history_baseline_keeps_month_end_points_and_counts_every_evaluation() -> None:
    baseline = build_history_baseline(
        [
            _line("2020-03-10", "0.20", "0.05"),
            _line("2020-03-31", "0.04", "0.17"),
            "",
            _line("2020-04-30", "-0.55", "-0.31"),
            _line("2020-05-15", None, "-0.40"),
        ]
    )
    assert baseline["baseline_version"] == HISTORY_BASELINE_VERSION
    assert baseline["evaluation_count"] == 4
    assert baseline["first_evaluation_as_of"] == "2020-03-10"
    assert baseline["last_evaluation_as_of"] == "2020-05-15"
    assert [item["d"] for item in baseline["month_end_points"]] == [
        "2020-03-31",
        "2020-04-30",
        "2020-05-15",
    ]
    assert baseline["month_end_points"][0]["g"] == 0.04
    assert baseline["month_end_points"][2]["g"] is None
    assert baseline["regime_counts"] == {
        "NEUTRAL|HIGH_PRESSURE": 1,
        "STRONG|NEUTRAL_PRESSURE": 1,
        "WEAK|LOW_PRESSURE": 1,
    }


def test_empty_history_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="no evaluations"):
        build_history_baseline(["", "  "])


def test_scores_are_formatted_with_an_explicit_sign_and_no_long_decimals() -> None:
    assert _signed(0.051456098259939961) == "+0.05"
    assert _signed(-0.0599) == "−0.06"
    assert _signed(0.0004) == "0.00"
    assert _signed(-0.0871, 3) == "−0.087"
    assert _signed(None) == "无值"


def test_page_embeds_data_once_and_cannot_be_closed_by_its_own_payload() -> None:
    data = {"text": {"verdict": "</script><b>x</b>"}, "meta": {"as_of": "2026-11-14"}}
    fragment = render_visual_report(data, standalone=False)
    assert "/*DATA*/" not in fragment
    assert "<\\/script><b>x<\\/b>" in fragment
    assert not fragment.lstrip().lower().startswith("<!doctype")
    assert fragment.count("</script>") == 1

    page = render_visual_report(data)
    assert page.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in page
    assert fragment in page
