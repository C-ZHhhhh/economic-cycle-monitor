"""Build the plain-language visual report from persisted model results.

Presentation only. Every number comes from a persisted Stage 3 context, the stored
history, or stored raw observations; nothing is rescaled, reweighted or
reclassified here, and no provider or LLM is called.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents.macro.explanation_loader import PERSISTED_CONTEXT_REPORT_TYPE
from agents.macro.explanation_schemas import (
    DeterministicDriver,
    LoadedExplanationContext,
    MacroExplanationSnapshot,
    PersistedCadence,
    PersistedExplanationContext,
)
from app.composites.config import load_composite_catalog
from app.composites.schemas import EvaluationMode
from app.database.models import AgentReport, AgentRun, RawObservation
from app.database.repositories.runs import add_run_with_report
from app.pillars.schemas import PillarResult
from app.signals.schemas import SignalQualityFlag
from app.source_registry.config import load_source_registry, storage_source_name
from app.source_registry.permanent_gaps import load_permanent_gap_catalog
from app.source_registry.schemas import SourceRegistryCatalog

DEFAULT_REPORT_GLOSSARY_PATH = Path("config/reports/us_macro_report_zh.yaml")
TEMPLATE_PATH = Path(__file__).with_name("templates") / "visual_report.html"
HISTORY_BASELINE_REPORT_TYPE = "macro_regime_history_baseline"
HISTORY_BASELINE_VERSION = "macro-regime-history-baseline-v1"
_NEAR_BOUNDARY = Decimal("0.05")
_NEAR_NORM = Decimal("0.10")

_GROWTH_ZH = {"WEAK": "偏弱", "NEUTRAL": "中性", "STRONG": "偏强"}
_INFLATION_ZH = {"LOW_PRESSURE": "偏低", "NEUTRAL_PRESSURE": "中性", "HIGH_PRESSURE": "偏高"}
_GROWTH_TRAJECTORY_ZH = {
    "DETERIORATING": "正在走弱",
    "STABLE": "基本持平",
    "IMPROVING": "正在回升",
}
_INFLATION_TRAJECTORY_ZH = {"FALLING": "正在回落", "STABLE": "基本持平", "RISING": "正在上升"}


class TermEntry(BaseModel):
    """Display name and one-line explanation for one stable ID."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    what: str = Field(min_length=1)


class SmallMultipleSpec(BaseModel):
    """One raw series shown as a small chart."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    transform: Literal["level", "yoy", "monthly_mean", "four_week_mean"]
    scale: Decimal = Decimal(1)
    unit: str
    decimals: int = Field(ge=0, le=4)


class AbsoluteSeriesSpec(BaseModel):
    """One price index shown as year-over-year change beside the target."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    label: str


class ReportGlossary(BaseModel):
    """Presentation-only wording and chart selection for the visual report."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    report_glossary_version: str
    inflation_target_percent: Decimal
    inflation_target_label: str
    stale_after_days: int = Field(ge=0)
    labor_pillar_id: str
    sources: dict[str, str]
    pillars: dict[str, TermEntry]
    indicators: dict[str, TermEntry]
    transforms: dict[str, str]
    regimes: dict[str, str]
    small_multiples: list[SmallMultipleSpec]
    absolute_inflation: list[AbsoluteSeriesSpec]


def load_report_glossary(path: Path = DEFAULT_REPORT_GLOSSARY_PATH) -> ReportGlossary:
    """Load the report wording authority and require it to cover every model ID."""
    glossary = ReportGlossary.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    registry = load_source_registry()
    missing = sorted(set(registry.by_indicator()) - set(glossary.indicators))
    if missing:
        raise ValueError(f"report glossary is missing indicators: {missing}")
    composite = load_composite_catalog()
    pillar_ids = {
        *(item.pillar_id for item in composite.growth.pillars),
        composite.inflation_pillar_id,
        *composite.context_pillar_ids,
    }
    if pillar_ids - set(glossary.pillars) or glossary.labor_pillar_id not in pillar_ids:
        raise ValueError("report glossary does not cover every pillar")
    return glossary


def _number(value: Decimal | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _signed(value: Decimal | float | None, digits: int = 2) -> str:
    """Format a score with an explicit sign and a typographic minus."""
    if value is None:
        return "无值"
    number = float(value)
    rounded = round(number, digits)
    sign = "+" if rounded > 0 else "−" if rounded < 0 else ""
    return f"{sign}{abs(rounded):.{digits}f}"


def _zh_date(value: date) -> str:
    return f"{value.year} 年 {value.month} 月 {value.day} 日"


def _spaced(name: str) -> str:
    """Follow a Latin-script name with a space before Chinese text."""
    return f"{name} " if name.isascii() else name


def _short_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.month} 月 {parsed.day} 日"


def _zh_month(value: date) -> str:
    return f"{value.year} 年 {value.month} 月"


# ---------------------------------------------------------------- history


def build_history_baseline(lines: Iterable[str]) -> dict[str, Any]:
    """Condense a Stage 3F production-results stream into chartable history.

    Keeps the last evaluation of each month for the line charts and counts every
    evaluation by base regime for the frequency grid.
    """
    by_month: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    dates: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        regime = json.loads(line)["regime"]
        evaluation_as_of = str(regime["evaluation_as_of"])
        growth = regime.get("growth_strength")
        inflation = regime.get("inflation_pressure")
        growth_condition = regime.get("growth_condition")
        inflation_condition = regime.get("inflation_condition")
        dates.append(evaluation_as_of)
        if growth_condition and inflation_condition:
            counts[f"{growth_condition}|{inflation_condition}"] += 1
        by_month[evaluation_as_of[:7]] = {
            "d": evaluation_as_of,
            "g": None if growth is None else round(float(growth), 4),
            "i": None if inflation is None else round(float(inflation), 4),
            "gc": growth_condition,
            "ic": inflation_condition,
        }
    if not dates:
        raise ValueError("history source contains no evaluations")
    return {
        "baseline_version": HISTORY_BASELINE_VERSION,
        "first_evaluation_as_of": min(dates),
        "last_evaluation_as_of": max(dates),
        "evaluation_count": len(dates),
        "regime_counts": dict(sorted(counts.items())),
        "month_end_points": [by_month[key] for key in sorted(by_month)],
    }


def persist_history_baseline(
    session: Session, baseline: dict[str, Any], *, git_commit: str | None = None
) -> UUID:
    """Store one history baseline; the newest stored baseline is the one used."""
    if baseline.get("baseline_version") != HISTORY_BASELINE_VERSION:
        raise ValueError("HISTORY_BASELINE_VERSION_MISMATCH")
    now = datetime.now(UTC)
    run_id = uuid4()
    run = AgentRun(
        run_id=run_id,
        agent_name="us_macro_regime_history_baseline",
        agent_version=HISTORY_BASELINE_VERSION,
        status="completed",
        started_at=now,
        finished_at=now,
        duration_seconds=Decimal(0),
        llm_mode="none",
        input_tokens=0,
        output_tokens=0,
        estimated_cost=Decimal(0),
        git_commit=git_commit,
    )
    report = AgentReport(
        run_id=run_id,
        report_type=HISTORY_BASELINE_REPORT_TYPE,
        result_json=baseline,
        prompt_version=None,
    )
    add_run_with_report(session, run, report)
    return run_id


def load_history_baseline(session: Session) -> dict[str, Any] | None:
    """Return the newest stored history baseline, if one was imported."""
    report = session.scalar(
        select(AgentReport)
        .where(AgentReport.report_type == HISTORY_BASELINE_REPORT_TYPE)
        .order_by(AgentReport.created_at.desc(), AgentReport.id.desc())
        .limit(1)
    )
    return dict(report.result_json) if report is not None else None


def current_snapshot_points(session: Session) -> list[dict[str, Any]]:
    """Return one point per date from every stored current-mode snapshot."""
    regime = AgentReport.result_json["regime"]
    rows = session.execute(
        select(
            regime["evaluation_as_of"].astext,
            regime["growth_strength"].astext,
            regime["inflation_pressure"].astext,
            regime["growth_condition"].astext,
            regime["inflation_condition"].astext,
        )
        .where(
            AgentReport.report_type == PERSISTED_CONTEXT_REPORT_TYPE,
            AgentReport.result_json["cadence"].astext == PersistedCadence.RELEASE_EVENT_EOD.value,
            regime["evaluation_mode"].astext == EvaluationMode.CURRENT.value,
        )
        .order_by(AgentReport.created_at)
    ).all()
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        evaluation_as_of = str(row[0])
        by_date[evaluation_as_of] = {
            "d": evaluation_as_of,
            "g": None if row[1] is None else round(float(str(row[1])), 4),
            "i": None if row[2] is None else round(float(str(row[2])), 4),
            "gc": row[3],
            "ic": row[4],
            "snap": True,
        }
    return [by_date[key] for key in sorted(by_date)]


# ---------------------------------------------------------------- raw series


def _stored(session: Session, indicator_id: str, source: str) -> list[tuple[date, Decimal]]:
    rows = session.execute(
        select(RawObservation.observation_date, RawObservation.value)
        .where(RawObservation.indicator_id == indicator_id, RawObservation.source == source)
        .order_by(RawObservation.observation_date)
    ).all()
    return [(row[0], Decimal(row[1])) for row in rows]


def _raw_series(
    session: Session, registry: SourceRegistryCatalog, indicator_id: str
) -> tuple[list[tuple[date, Decimal]], date | None]:
    """Read stored observations for display, newest available from either source.

    The primary source is used wherever it has data. When the fallback already
    holds newer periods, those are appended so a chart shows the latest published
    number; the first appended date is returned so the page can say which part is
    not what the model used. This never feeds a calculation.
    """
    policy = registry.by_indicator()[indicator_id]
    rows = _stored(session, indicator_id, storage_source_name(policy.primary_source.value))
    if policy.fallback_source is None:
        return rows, None
    fallback = _stored(session, indicator_id, storage_source_name(policy.fallback_source.value))
    if not rows:
        return fallback, None
    newer = [item for item in fallback if item[0] > rows[-1][0]]
    return [*rows, *newer], (newer[0][0] if newer else None)


def _source_lags(
    session: Session, registry: SourceRegistryCatalog, glossary: ReportGlossary
) -> list[dict[str, Any]]:
    """List indicators whose fallback already holds a newer period than the primary."""
    latest: dict[tuple[str, str], date] = {
        (row[0], row[1]): row[2]
        for row in session.execute(
            select(
                RawObservation.indicator_id,
                RawObservation.source,
                func.max(RawObservation.observation_date),
            ).group_by(RawObservation.indicator_id, RawObservation.source)
        ).all()
    }
    lags: list[dict[str, Any]] = []
    for policy in registry.policies:
        if policy.fallback_source is None:
            continue
        primary_source = storage_source_name(policy.primary_source.value)
        fallback_source = storage_source_name(policy.fallback_source.value)
        primary = latest.get((policy.indicator_id, primary_source))
        fallback = latest.get((policy.indicator_id, fallback_source))
        if primary is None or fallback is None or fallback <= primary:
            continue
        lags.append(
            {
                "ind": policy.indicator_id,
                "name": glossary.indicators[policy.indicator_id].name,
                "primary": glossary.sources.get(primary_source, primary_source),
                "primary_last": primary.isoformat(),
                "fallback": glossary.sources.get(fallback_source, fallback_source),
                "fallback_last": fallback.isoformat(),
                "grace_days": policy.freshness.grace_days,
            }
        )
    return lags


def _year_over_year(rows: Sequence[tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    by_date = dict(rows)
    result: list[tuple[date, Decimal]] = []
    for observed, value in rows:
        prior = by_date.get(observed.replace(year=observed.year - 1))
        if prior is not None and prior > 0:
            result.append((observed, (value / prior - 1) * 100))
    return result


def _monthly_mean(rows: Sequence[tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    buckets: dict[date, list[Decimal]] = {}
    for observed, value in rows:
        buckets.setdefault(observed.replace(day=1), []).append(value)
    return [(month, sum(values, Decimal(0)) / len(values)) for month, values in buckets.items()]


def _four_week_mean(rows: Sequence[tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    return [
        (rows[index][0], sum((item[1] for item in rows[index - 3 : index + 1]), Decimal(0)) / 4)
        for index in range(3, len(rows))
    ]


def _points(
    rows: Sequence[tuple[date, Decimal]], *, start: date, digits: int, scale: Decimal = Decimal(1)
) -> list[list[Any]]:
    return [
        [observed.isoformat(), round(float(value * scale), digits)]
        for observed, value in rows
        if observed >= start
    ]


# ---------------------------------------------------------------- assembly


def _transform_key(signal_id: str, indicator_id: str) -> str:
    return signal_id[len(indicator_id) + 2 :].split("__")[0]


def _pillar_payload(pillar: PillarResult, glossary: ReportGlossary) -> dict[str, Any]:
    term = glossary.pillars[pillar.pillar_id]
    signals: list[dict[str, Any]] = []
    for signal_id in pillar.constituent_signal_ids:
        indicator_id = signal_id.split("__")[0]
        entry = glossary.indicators[indicator_id]
        observed = pillar.constituent_observation_dates.get(signal_id)
        flags = pillar.constituent_quality_flags.get(signal_id, [])
        signals.append(
            {
                "id": signal_id,
                "ind": indicator_id,
                "name": entry.name,
                "what": entry.what,
                "xf": glossary.transforms.get(_transform_key(signal_id, indicator_id), ""),
                "obs": observed.isoformat() if observed else None,
                "z": _number(pillar.constituent_normalized_values.get(signal_id), 3),
                "c": _number(pillar.constituent_contributions.get(signal_id), 3),
                "st": pillar.constituent_statuses.get(signal_id, ""),
                "gap": SignalQualityFlag.PERMANENT_GAP_SKIPPED.value in flags,
            }
        )
    return {
        "id": pillar.pillar_id,
        "name": term.name,
        "what": term.what,
        "v": _number(pillar.value, 3),
        "conf": _number(pillar.confidence, 2),
        "ok": pillar.valid_constituent_count,
        "n": pillar.expected_constituent_count,
        "st": pillar.pillar_status.value,
        "sig": signals,
    }


def _driver_payload(driver: DeterministicDriver, glossary: ReportGlossary) -> dict[str, Any]:
    entry = glossary.indicators[driver.indicator_id]
    return {
        "ind": driver.indicator_id,
        "name": entry.name,
        "what": entry.what,
        "xf": glossary.transforms.get(_transform_key(driver.signal_id, driver.indicator_id), ""),
        "pillar": driver.pillar_id,
        "pillar_name": glossary.pillars[driver.pillar_id].name,
        "v": _number(driver.attributed_impact, 4),
    }


def _regime_label(growth_condition: str | None, inflation_condition: str | None) -> str:
    if growth_condition is None or inflation_condition is None:
        return "无法分类"
    return f"增长{_GROWTH_ZH[growth_condition]} / 通胀{_INFLATION_ZH[inflation_condition]}"


def _share_below(values: Sequence[float], current: float) -> float:
    return 100 * sum(item < current for item in values) / len(values) if values else 0.0


def _change_rows(
    current: PersistedExplanationContext,
    previous: PersistedExplanationContext,
    glossary: ReportGlossary,
    pillar_order: Sequence[str],
) -> list[dict[str, Any]]:
    before = {item.pillar_id: item.value for item in previous.pillars}
    now = {item.pillar_id: item.value for item in current.pillars}
    rows: list[dict[str, Any]] = [
        {
            "label": "增长得分",
            "sub": "总分",
            "prev": _number(previous.regime.growth_strength),
            "now": _number(current.regime.growth_strength),
        },
        {
            "label": "通胀压力得分",
            "sub": "总分",
            "prev": _number(previous.regime.inflation_pressure),
            "now": _number(current.regime.inflation_pressure),
        },
    ]
    rows.extend(
        {
            "label": glossary.pillars[pillar_id].name,
            "sub": "支柱",
            "prev": _number(before.get(pillar_id)),
            "now": _number(now.get(pillar_id)),
        }
        for pillar_id in pillar_order
    )
    return rows


def build_report_data(
    session: Session,
    *,
    loaded: LoadedExplanationContext,
    snapshot: MacroExplanationSnapshot,
    glossary: ReportGlossary,
    generated_on: date,
) -> dict[str, Any]:
    """Assemble every number and sentence the visual report shows."""
    current = loaded.current
    if current is None:
        raise RuntimeError("UPSTREAM_RESULT_NOT_MATERIALIZED")
    regime = current.regime
    registry = load_source_registry()
    composite = load_composite_catalog()
    gaps = load_permanent_gap_catalog(source_registry=registry)
    growth_ids = [item.pillar_id for item in composite.growth.pillars]
    context_ids = list(composite.context_pillar_ids)
    inflation_id = composite.inflation_pillar_id
    as_of = regime.evaluation_as_of
    stale_days = max(0, (generated_on - as_of).days)
    stale = stale_days > glossary.stale_after_days

    pillars = {item.pillar_id: _pillar_payload(item, glossary) for item in snapshot.pillars}
    growth_drivers = [_driver_payload(item, glossary) for item in snapshot.drivers.full_growth]
    inflation_drivers = [
        _driver_payload(item, glossary) for item in snapshot.drivers.full_inflation
    ]
    growth_condition = regime.growth_condition.value if regime.growth_condition else None
    inflation_condition = regime.inflation_condition.value if regime.inflation_condition else None
    growth_trajectory = regime.growth_trajectory
    inflation_trajectory = regime.inflation_trajectory
    readiness = current.readiness
    health = [
        {
            "ind": item.indicator_id,
            "name": glossary.indicators[item.indicator_id].name,
            "src": glossary.sources.get(item.source, item.source),
            "obs": item.latest_observation_date.isoformat()
            if item.latest_observation_date
            else None,
            "ind_id": item.indicator_id,
        }
        for item in current.health
        if item.indicator_id is not None
    ]

    # ---- history
    baseline = load_history_baseline(session)
    history: list[dict[str, Any]] = list(baseline["month_end_points"]) if baseline else []
    seen = {item["d"] for item in history}
    history.extend(item for item in current_snapshot_points(session) if item["d"] not in seen)
    history.sort(key=lambda item: str(item["d"]))
    month_ends = baseline["month_end_points"] if baseline else []

    # ---- raw series
    lags = _source_lags(session, registry, glossary)
    lag_by_indicator = {item["ind"]: item for item in lags}
    series_start = date(as_of.year - 4, 1, 1)
    small: list[dict[str, Any]] = []
    for spec in glossary.small_multiples:
        rows, fallback_from = _raw_series(session, registry, spec.indicator_id)
        weekly = registry.by_indicator()[spec.indicator_id].canonical.frequency == "weekly"
        if spec.transform == "yoy":
            rows = _year_over_year(rows)
        elif spec.transform == "monthly_mean":
            rows = _monthly_mean(rows)
        elif spec.transform == "four_week_mean":
            rows = _four_week_mean(rows)
        points = _points(rows, start=series_start, digits=spec.decimals, scale=spec.scale)
        if len(points) > 1:
            entry = glossary.indicators[spec.indicator_id]
            last = date.fromisoformat(points[-1][0])
            by_week = weekly and spec.transform != "monthly_mean"
            lag = lag_by_indicator.get(spec.indicator_id)
            small.append(
                {
                    "name": entry.name + ("同比" if spec.transform == "yoy" else ""),
                    "what": entry.what,
                    "unit": spec.unit,
                    "nd": spec.decimals,
                    "when": f"{last.month} 月 {last.day} 日当周" if by_week else f"{last.month} 月",
                    "weekly": by_week,
                    "src": (
                        f"{lag['primary']}的数据只到 {_short_date(lag['primary_last'])}，"
                        f"之后的部分取自 {lag['fallback']}。"
                    )
                    if fallback_from is not None and lag is not None
                    else "",
                    "pts": points,
                }
            )
    absolute: list[dict[str, Any]] = []
    for price_spec in glossary.absolute_inflation:
        points = _points(
            _year_over_year(_raw_series(session, registry, price_spec.indicator_id)[0]),
            start=date(as_of.year - 7, 1, 1),
            digits=2,
        )
        if len(points) > 1:
            absolute.append({"label": price_spec.label, "pts": points})

    for entry_health in health:
        behind = lag_by_indicator.get(str(entry_health.pop("ind_id")))
        entry_health["alt"] = (
            f"{_spaced(behind['fallback'])}已到 {behind['fallback_last']}"
            if behind is not None
            else ""
        )

    # ---- comparison with the previous snapshot
    previous = loaded.previous_release_eod
    prev_payload: dict[str, Any] | None = None
    if previous is not None:
        prev_payload = {
            "date": previous.evaluation_as_of.isoformat(),
            "rows": _change_rows(current, previous, glossary, [*growth_ids, *context_ids]),
        }

    # ---- sentences
    text: dict[str, Any] = {}
    growth_zh = _GROWTH_ZH.get(growth_condition or "", "无法判断")
    inflation_zh = _INFLATION_ZH.get(inflation_condition or "", "无法判断")
    growth_move = _GROWTH_TRAJECTORY_ZH.get(
        growth_trajectory.qualifier.value if growth_trajectory.qualifier else "", "趋势暂无法判断"
    )
    inflation_move = _INFLATION_TRAJECTORY_ZH.get(
        inflation_trajectory.qualifier.value if inflation_trajectory.qualifier else "",
        "趋势暂无法判断",
    )
    text["dateline"] = f"数据截至 {_zh_date(as_of)}　·　报告生成于 {_zh_date(generated_on)}"
    text["verdict"] = f"增长{growth_zh}，{growth_move}；通胀压力{inflation_zh}，{inflation_move}。"
    text["stale"] = (
        f"<b>这是一张 {_zh_date(as_of)}的快照，不是今天的判断。</b>"
        f"距今已 {stale_days} 天，之后发布的数据没有计入。"
        "重新运行 run-macro-report 可以更新。"
    )
    text["g_head"] = (
        f'增长得分 <span class="num">{_signed(regime.growth_strength)}</span>'
        f"　{growth_zh}，{growth_move}"
    )
    text["i_head"] = (
        f'通胀压力得分 <span class="num">{_signed(regime.inflation_pressure)}</span>'
        f"　{inflation_zh}，{inflation_move}"
    )
    growth_margin = regime.growth_classification_margin
    inflation_margin = regime.inflation_classification_margin
    text["g_cap"] = (
        f"离最近的分界线 {float(growth_margin):.2f}。"
        if growth_margin is not None
        else "数据不足，无法分类。"
    )
    text["i_cap"] = (
        f"离最近的分界线 {float(inflation_margin):.2f}。"
        if inflation_margin is not None
        else "数据不足，无法分类。"
    )

    scored_growth = [pillars[item] for item in growth_ids if pillars[item]["v"] is not None]
    missing_growth = [pillars[item]["name"] for item in growth_ids if pillars[item]["v"] is None]
    strongest = max(scored_growth, key=lambda item: item["v"], default=None)
    weakest = min(scored_growth, key=lambda item: item["v"], default=None)
    positive_inflation = sorted(
        (item for item in inflation_drivers if item["v"] > 0), key=lambda item: -item["v"]
    )
    negative_inflation = sorted(
        (item for item in inflation_drivers if item["v"] < 0), key=lambda item: item["v"]
    )

    means = [glossary.regimes.get(f"{growth_condition}|{inflation_condition}", "")]
    if growth_trajectory.lag_value is not None and inflation_trajectory.lag_value is not None:
        means.append(
            f"和 3 个月前（{_zh_date(growth_trajectory.lag_as_of)}）相比，增长得分从 "
            f"{_signed(growth_trajectory.lag_value)} 变为 {_signed(regime.growth_strength)}，"
            f"通胀压力得分从 {_signed(inflation_trajectory.lag_value)} 变为 "
            f"{_signed(regime.inflation_pressure)}。"
        )
    drivers_sentence = ""
    if strongest is not None and weakest is not None and strongest is not weakest:
        drivers_sentence += (
            f"增长这边，最强的一块是{strongest['name']}（{_signed(strongest['v'])}），"
            f"最弱的是{weakest['name']}（{_signed(weakest['v'])}）。"
        )
    if positive_inflation:
        drivers_sentence += f"物价这边，推高压力最多的是{positive_inflation[0]['name']}"
        drivers_sentence += (
            f"，压低最多的是{negative_inflation[0]['name']}。" if negative_inflation else "。"
        )
    if drivers_sentence:
        means.append(drivers_sentence)
    text["means"] = [item for item in means if item]

    latest_absolute = [
        (item["label"], item["pts"][-1][1], date.fromisoformat(item["pts"][-1][0]))
        for item in absolute
    ]
    target = float(glossary.inflation_target_percent)
    caveats: list[str] = []
    if missing_growth:
        caveats.append(f"{'、'.join(missing_growth)}数据不足，没有计入增长得分。")
    if latest_absolute:
        label, value, observed = latest_absolute[0]
        if inflation_condition != "HIGH_PRESSURE" and value > target + 0.5:
            caveats.append(
                f"通胀压力得分是和过去十年比出来的。按实际涨幅看，{_zh_month(observed)} "
                f"{label} 同比 {value:.2f}%，仍高于 {target:g}% 的目标。"
            )
        elif inflation_condition == "HIGH_PRESSURE" and value <= target + 0.5:
            caveats.append(
                f"通胀压力得分偏高，但 {_zh_month(observed)} {label} 同比是 {value:.2f}%，"
                f"离 {target:g}% 的目标不远：得分反映的是近几个月涨价在加快。"
            )
    for axis, margin in (("增长", growth_margin), ("通胀压力", inflation_margin)):
        if margin is not None and margin <= _NEAR_BOUNDARY:
            caveats.append(f"{axis}得分离分界线只有 {float(margin):.2f}，小幅变动就会换档。")
    if readiness is not None and not readiness.current_data_ready:
        caveats.append(
            f"有 {len(readiness.current_blockers)} 项数据没有通过新鲜度或一致性检查，结论仅供参考。"
        )
    text["caveats"] = caveats

    # change
    if previous is None or prev_payload is None:
        text["change_answer"] = "这是库里的第一份快照。下次运行起，这里会显示和上一次的差别。"
        text["change_title"] = ""
        text["change_foot"] = ""
    else:
        previous_regime = previous.regime
        previous_label = _regime_label(
            previous_regime.growth_condition.value if previous_regime.growth_condition else None,
            previous_regime.inflation_condition.value
            if previous_regime.inflation_condition
            else None,
        )
        label_now = _regime_label(growth_condition, inflation_condition)
        days = (as_of - previous.evaluation_as_of).days
        tail = (
            f"状态没有换档，仍是“{label_now}”。"
            if previous_label == label_now
            else f"状态从“{previous_label}”变为“{label_now}”。"
        )
        text["change_answer"] = (
            f"上一次快照是 {_zh_date(previous.evaluation_as_of)}（{days} 天前）。"
            f"增长得分 {_signed(previous_regime.growth_strength)} → "
            f"{_signed(regime.growth_strength)}，通胀压力得分 "
            f"{_signed(previous_regime.inflation_pressure)} → "
            f"{_signed(regime.inflation_pressure)}。{tail}"
        )
        text["change_title"] = f"{_zh_date(previous.evaluation_as_of)} → {_zh_date(as_of)}"
        restored = [
            row["label"]
            for row in prev_payload["rows"]
            if row["sub"] == "支柱" and row["prev"] is None and row["now"] is not None
        ]
        text["change_foot"] = (
            f"{'、'.join(restored)}上一次没有分数，这一次有了，所以前后两次的总分不完全可比。"
            if restored
            else ""
        )

    # path and history
    path = [item for item in history if item["g"] is not None and item["i"] is not None]
    recent = [item for item in path if str(item["d"]) < as_of.isoformat()][-12:]
    labels: list[str] = []
    for point in recent:
        label = _regime_label(point["gc"], point["ic"])
        if not labels or labels[-1] != label:
            labels.append(label)
    label_now = _regime_label(growth_condition, inflation_condition)
    distinct = len({*labels, label_now})
    if not recent:
        text["path_answer"] = "还没有足够的历史来画出路径。"
    elif distinct == 1:
        text["path_answer"] = f"过去一年一直停在“{label_now}”这一格。"
    else:
        text["path_answer"] = f"过去一年经过了 {distinct} 种状态，现在落在“{label_now}”。"
    if baseline is not None:
        total = int(baseline["evaluation_count"])
        share = (
            100
            * baseline["regime_counts"].get(f"{growth_condition}|{inflation_condition}", 0)
            / max(total, 1)
        )
        first = date.fromisoformat(baseline["first_evaluation_as_of"])
        last = date.fromisoformat(baseline["last_evaluation_as_of"])
        text["freq_cap"] = (
            f"{_zh_month(first)}到 {_zh_month(last)}共 {total:,} 个计算日，每格是所占比例。"
            "黑框是现在所在的格。"
        )
        text["freq_foot"] = f"现在所在的“{label_now}”在这段历史里占 {share:.1f}%。"
        growth_values = [float(item["g"]) for item in month_ends if item["g"] is not None]
        inflation_values = [float(item["i"]) for item in month_ends if item["i"] is not None]
        if regime.growth_strength is not None and regime.inflation_pressure is not None:
            text["hist_answer"] = (
                f"增长得分比历史上 {_share_below(growth_values, float(regime.growth_strength)):.0f}% "
                f"的月份高，通胀压力得分比 "
                f"{_share_below(inflation_values, float(regime.inflation_pressure)):.0f}% 的月份高。"
            )
        else:
            text["hist_answer"] = "这次没有完整的得分，无法和历史比较。"
        low = min((item for item in month_ends if item["g"] is not None), key=lambda x: x["g"])
        high = max((item for item in month_ends if item["i"] is not None), key=lambda x: x["i"])
        text["hist_foot"] = (
            f"月末数据里，增长得分最低是 {_signed(low['g'])}"
            f"（{_zh_month(date.fromisoformat(low['d']))}），通胀压力得分最高是 "
            f"{_signed(high['i'])}（{_zh_month(date.fromisoformat(high['d']))}）。"
            "拿这两个极端做参照，可以看出现在离“很冷”或“很热”有多远。"
        )
        text["hist_note"] = (
            f"<b>关于历史曲线。</b>{_zh_month(first)}至 {_zh_month(last)}的部分来自历史回放"
            f"（共 {total:,} 个计算日，每个日期只使用当时已经公布的数据）；"
            "之后的点来自每次运行存下的快照。"
        )
    else:
        text["freq_cap"] = text["freq_foot"] = text["hist_foot"] = text["hist_note"] = ""
        text["hist_answer"] = "下面是已存快照连成的曲线。"
    years = (f"{str(history[0]['d'])[:4]}–{str(history[-1]['d'])[:4]}") if history else ""
    text["hist_g_title"] = f"增长得分，{years}"
    text["hist_i_title"] = f"通胀压力得分，{years}"

    # growth
    if strongest is None or weakest is None:
        text["growth_answer"] = "增长的四根支柱这次都没有分数。"
    elif strongest is weakest:
        text["growth_answer"] = (
            f"只有{strongest['name']}有分数（{_signed(strongest['v'])}），其余支柱数据不足。"
        )
    else:
        text["growth_answer"] = (
            f"最强的是{strongest['name']}（{_signed(strongest['v'])}），"
            f"最弱的是{weakest['name']}（{_signed(weakest['v'])}）；"
            f"{len(scored_growth)} 根支柱平均下来是 {_signed(regime.growth_strength)}。"
        )
    text["pillar_cap"] = (
        f"模型把增长拆成四块，各自打分，再取平均。这次 {len(scored_growth)} 块有分数。"
    )
    text["driver_cap"] = (
        f"每根柱子是一项数据对增长得分的贡献，{len(growth_drivers)} 根加起来等于 "
        f"{_signed(regime.growth_strength)}。"
    )
    ranked = sorted(growth_drivers, key=lambda item: item["v"])
    growth_points = [
        f"<b>{item['name']}</b>在推高增长（{_signed(item['v'], 3)}）。{item['what']}。"
        for item in reversed(ranked[-2:])
        if item["v"] > 0
    ]
    growth_points.extend(
        f"<b>{item['name']}</b>在拉低增长（{_signed(item['v'], 3)}）。{item['what']}。"
        for item in ranked[:2]
        if item["v"] < 0
    )
    text["growth_points"] = growth_points

    # labor
    labor = pillars[glossary.labor_pillar_id]
    labor_signals = [item for item in labor["sig"] if item["c"] is not None]
    text["labor_title"] = f"劳动力支柱的 {labor['n']} 个信号"
    if labor["v"] is None:
        text["labor_answer"] = (
            f"劳动力支柱这次没有分数：{labor['n']} 个信号里只有 {labor['ok']} 个可用。"
        )
    elif len(labor_signals) > 1:
        top = max(labor_signals, key=lambda item: item["c"])
        bottom = min(labor_signals, key=lambda item: item["c"])
        text["labor_answer"] = (
            f"劳动力支柱得分 {_signed(labor['v'])}，{labor['ok']}/{labor['n']} 个信号可用。"
            f"最强的信号是{top['name']}（{_signed(top['c'])}），"
            f"最弱的是{bottom['name']}（{_signed(bottom['c'])}）。"
        )
    else:
        text["labor_answer"] = f"劳动力支柱得分 {_signed(labor['v'])}。"
    skipped = sorted(
        {signal["ind"] for pillar in pillars.values() for signal in pillar["sig"] if signal["gap"]}
    )
    gap_periods = sorted({period for item in skipped for period in gaps.periods_for(item)})
    gap_sentence = ""
    if skipped and gap_periods:
        months = "、".join(_zh_month(item) for item in gap_periods)
        gap_sentence = (
            f"{len(skipped)} 项数据缺 {months}（官方确认不会发布）。模型在计算常态时跳过这些月份，"
            "不填补任何数值，其余照常计算。"
        )
    labor_gap = [item["name"] for item in labor["sig"] if item["gap"]]
    text["labor_foot"] = (
        f"{'、'.join(labor_gap)}的历史里有官方永久缺失的月份，已按规则跳过。" if labor_gap else ""
    )

    # inflation
    inflation = pillars[inflation_id]
    text["infl_title"] = f"通胀压力的 {inflation['n']} 个信号"
    text["infl_cap"] = (
        f"{inflation['ok']} 个可用，加起来是 {_signed(regime.inflation_pressure)}。"
        "红色在推高压力，蓝色在压低压力。"
    )
    if positive_inflation and negative_inflation:
        text["infl_answer"] = (
            f"推高压力最多的是{positive_inflation[0]['name']}"
            f"（{_signed(positive_inflation[0]['v'], 3)}），压低最多的是"
            f"{negative_inflation[0]['name']}（{_signed(negative_inflation[0]['v'], 3)}）。"
        )
    elif positive_inflation:
        text["infl_answer"] = (
            f"可用的 {len(inflation_drivers)} 个信号都在推高压力，最多的是"
            f"{positive_inflation[0]['name']}（{_signed(positive_inflation[0]['v'], 3)}）。"
        )
    elif negative_inflation:
        text["infl_answer"] = (
            f"可用的 {len(inflation_drivers)} 个信号都在压低压力，最多的是"
            f"{negative_inflation[0]['name']}（{_signed(negative_inflation[0]['v'], 3)}）。"
        )
    else:
        text["infl_answer"] = "通胀压力这次没有可用的信号。"
    if latest_absolute:
        observed = latest_absolute[0][2]
        readings = "，".join(f"{label} 同比 {value:.2f}%" for label, value, _ in latest_absolute)
        headline = latest_absolute[0][1]
        gap_to_target = (
            "明显高于目标"
            if headline > target + 0.5
            else "略高于目标"
            if headline > target + 0.1
            else "已经接近或低于目标"
        )
        text["pce_title"] = f"PCE 物价同比涨幅，{absolute[0]['pts'][0][0][:4]}–{observed.year}"
        text["pce_foot"] = (
            f"{_zh_month(observed)}，{readings}。目标是 {target:g}%，现在{gap_to_target}。"
        )
    else:
        text["pce_title"] = text["pce_foot"] = ""

    # context
    context_points: list[str] = []
    for pillar_id in context_ids:
        pillar = pillars[pillar_id]
        if pillar["v"] is None:
            continue
        value = Decimal(str(pillar["v"]))
        level = "接近常态" if abs(value) < _NEAR_NORM else "高于常态" if value > 0 else "低于常态"
        context_points.append(
            f"<b>{pillar['name']}</b> {_signed(pillar['v'])}，{level}。{pillar['what']}。"
        )
    text["ctx_points"] = context_points

    # reliability
    growth_confidence = float(regime.growth_data_confidence)
    inflation_confidence = float(regime.inflation_data_confidence)
    margins = [item for item in (growth_margin, inflation_margin) if item is not None]
    nearest = min(margins, default=None)
    blockers = len(readiness.current_blockers) if readiness is not None else 0
    ready = readiness is not None and readiness.current_data_ready
    stats = [
        {
            "k": "增长的数据可信度",
            "v": f"{growth_confidence:.2f}",
            "n": f"{len(scored_growth)}/{len(growth_ids)} 根增长支柱有分数。满分是 1。",
            "flag": "偏低" if growth_confidence < 0.9 else "",
        },
        {
            "k": "通胀的数据可信度",
            "v": f"{inflation_confidence:.2f}",
            "n": f"{inflation['ok']}/{inflation['n']} 个通胀信号可用。满分是 1。",
            "flag": "偏低" if inflation_confidence < 0.9 else "",
        },
        {
            "k": "离分界线最近",
            "v": f"{float(nearest):.2f}" if nearest is not None else "—",
            "n": "得分再变动这么多就会换档。低于 0.05 算贴线。",
            "flag": "贴线" if nearest is not None and nearest <= _NEAR_BOUNDARY else "",
        },
        {
            "k": "数据检查",
            "v": "通过" if ready else f"{blockers} 项受阻",
            "n": (
                f"快照距今 {stale_days} 天。"
                if stale_days
                else "所有数据都通过了新鲜度和来源一致性检查。"
                if ready
                else "部分数据过期或两个来源对不上。"
            ),
            "flag": "" if ready and not stale else "留意",
        },
    ]
    if lags:
        stats[3]["flag"] = "留意"
        if ready and not stale_days:
            stats[3]["n"] = f"检查都通过了，但有 {len(lags)} 项的主来源比备用来源旧。"
    reliability = list(caveats)
    if lags:
        names = "、".join(item["name"] for item in lags)
        oldest = min(lags, key=lambda item: item["primary_last"])
        newest = max(lags, key=lambda item: item["fallback_last"])
        reliability.append(
            f"{names}：模型用的是{oldest['primary']}的数据，只到 "
            f"{_short_date(oldest['primary_last'])}；{_spaced(newest['fallback'])}已经有 "
            f"{_short_date(newest['fallback_last'])}的数字。按来源规则，主来源落后超过宽限期"
            f"（{oldest['grace_days']} 天）才会改用备用来源，所以模型暂时没有用上最新几期。"
            "第 8 节的走势图已补上最新数字。"
        )
    if gap_sentence:
        reliability.append(gap_sentence)
    if growth_confidence < 0.9 or inflation_confidence < 0.9:
        reliability.append("可信度低于 0.9，说明有信号缺数据，得分只反映到手的那部分。")
    text["rel_points"] = reliability
    issues = sum(1 for item in stats if item["flag"])
    text["rel_answer"] = (
        "数据齐全，分类离分界线也有距离，这次的结论比较稳。"
        if issues == 0
        else f"有 {issues} 处需要留意，标在下面的卡片上。"
    )
    text["fresh_title"] = f"{len(health)} 项数据各自更新到了哪个月"
    text["source_note"] = (
        f"<b>数据来源。</b>当前状态由模型在 {_zh_date(as_of)}计算，"
        "原始数据来自 FRED、美国劳工统计局、经济分析局、人口普查局、美联储和劳工部。"
        "得分的算法和权重都在项目的 config 目录里，本报告不改动任何计算。"
    )

    return {
        "meta": {
            "as_of": as_of.isoformat(),
            "generated_on": generated_on.isoformat(),
            "stale": stale,
            "stale_days": stale_days,
            "target": target,
            "target_label": glossary.inflation_target_label,
        },
        "cur": {
            "as_of": as_of.isoformat(),
            "g": _number(regime.growth_strength),
            "i": _number(regime.inflation_pressure),
            "gc": growth_condition,
            "ic": inflation_condition,
            "traj": {
                "g": {
                    "lag_value": _number(growth_trajectory.lag_value),
                    "delta": _number(growth_trajectory.delta),
                },
                "i": {
                    "lag_value": _number(inflation_trajectory.lag_value),
                    "delta": _number(inflation_trajectory.delta),
                },
            },
            "pillars": list(pillars.values()),
            "growth_pillars": growth_ids,
            "context_pillars": context_ids,
            "labor_pillar": glossary.labor_pillar_id,
            "inflation_pillar": inflation_id,
            "gd": growth_drivers,
            "idr": inflation_drivers,
            "health": health,
        },
        "prev": prev_payload,
        "hist": history,
        "hist_meta": {"n_eod": int(baseline["evaluation_count"])} if baseline else None,
        "counts": baseline["regime_counts"] if baseline else {},
        "series": {"abs": absolute, "sm": small},
        "lags": lags,
        "stats": stats,
        "text": text,
    }


def render_visual_report(data: dict[str, Any], *, standalone: bool = True) -> str:
    """Embed report data into the page template.

    ``standalone`` wraps the page in a complete HTML document for opening from disk;
    without it the page content alone is returned for hosts that add their own shell.
    """
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count("/*DATA*/") != 1:
        raise RuntimeError("VISUAL_REPORT_TEMPLATE_INVALID")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = template.replace("/*DATA*/", payload)
    if not standalone:
        return page
    return (
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<style>body{margin:0}[hidden]{display:none!important}</style>\n</head>\n<body>\n"
        f"{page}\n</body>\n</html>\n"
    )
