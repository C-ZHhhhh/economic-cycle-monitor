"""PostgreSQL tests for snapshot storage, month-end promotion, and report assembly."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents.macro.current_run import persist_current_context, promote_previous_month_end
from agents.macro.explanation_config import load_explanation_authority
from agents.macro.explanation_loader import (
    PERSISTED_CONTEXT_REPORT_TYPE,
    PersistedExplanationContextLoader,
    seal_persisted_context,
)
from agents.macro.explanation_schemas import PersistedCadence, PersistedExplanationContext
from agents.macro.explanation_service import MacroExplanationService
from agents.macro.visual_report import (
    build_history_baseline,
    build_report_data,
    load_history_baseline,
    load_report_glossary,
    persist_history_baseline,
    render_visual_report,
)
from app.composites.schemas import EvaluationMode
from app.database.models import AgentReport, RawObservation
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog
from tests.unit.test_macro_explanation import _context

pytestmark = pytest.mark.integration


def _context_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(AgentReport)
            .where(AgentReport.report_type == PERSISTED_CONTEXT_REPORT_TYPE)
        )
        or 0
    )


def _dated(context: PersistedExplanationContext, when: date) -> PersistedExplanationContext:
    """Re-seal a fixture context as if it had been evaluated on another date."""
    values = context.model_dump(exclude={"persisted_context_hash"})
    values["regime"]["evaluation_as_of"] = when
    values["macro_state"]["evaluation_as_of"] = when
    for pillar in values["pillars"]:
        pillar["evaluation_as_of"] = when
    return seal_persisted_context(**values)


def test_identical_result_is_stored_once(postgres_session: Session) -> None:
    context = asyncio.run(_context())
    before = _context_count(postgres_session)
    assert persist_current_context(postgres_session, context) is not None
    assert persist_current_context(postgres_session, context) is None
    assert _context_count(postgres_session) == before + 1


def test_refetched_but_unchanged_result_is_not_stored_again(postgres_session: Session) -> None:
    context = asyncio.run(_context())
    values = context.model_dump(exclude={"persisted_context_hash"})
    values["regime"]["calculation_hash"] = "f" * 64
    refetched = seal_persisted_context(**values)
    assert refetched.persisted_context_hash != context.persisted_context_hash

    assert persist_current_context(postgres_session, context) is not None
    assert persist_current_context(postgres_session, refetched) is None


def test_last_snapshot_of_the_previous_month_becomes_its_closing_record(
    postgres_session: Session,
) -> None:
    today = date.today()
    earlier = today.replace(day=1) - timedelta(days=20)
    assert promote_previous_month_end(postgres_session, today) is None

    persist_current_context(postgres_session, _dated(asyncio.run(_context()), earlier))
    persist_current_context(postgres_session, asyncio.run(_context()))
    assert promote_previous_month_end(postgres_session, today) == earlier
    assert promote_previous_month_end(postgres_session, today) is None

    loaded = PersistedExplanationContextLoader(postgres_session).load(
        evaluation_mode=EvaluationMode.CURRENT
    )
    assert loaded.previous_release_eod is not None
    assert loaded.previous_release_eod.evaluation_as_of == earlier
    assert loaded.previous_month_end is not None
    assert loaded.previous_month_end.evaluation_as_of == earlier
    assert loaded.previous_month_end.cadence is PersistedCadence.MONTH_END_EOD


def test_report_is_assembled_from_persisted_results_and_renders(
    postgres_session: Session,
) -> None:
    today = date.today()
    earlier = today.replace(day=1) - timedelta(days=20)
    persist_current_context(postgres_session, _dated(asyncio.run(_context()), earlier))
    persist_current_context(postgres_session, asyncio.run(_context()))
    persist_history_baseline(
        postgres_session,
        build_history_baseline(
            [
                '{"regime": {"evaluation_as_of": "2020-04-30", "growth_strength": "-0.55",'
                ' "inflation_pressure": "-0.31", "growth_condition": "WEAK",'
                ' "inflation_condition": "LOW_PRESSURE"}}',
                '{"regime": {"evaluation_as_of": "2021-06-30", "growth_strength": "0.13",'
                ' "inflation_pressure": "0.90", "growth_condition": "STRONG",'
                ' "inflation_condition": "HIGH_PRESSURE"}}',
            ]
        ),
    )
    assert load_history_baseline(postgres_session) is not None

    service = MacroExplanationService(postgres_session, load_explanation_authority())
    data = build_report_data(
        postgres_session,
        loaded=service.loader.load(evaluation_mode=EvaluationMode.CURRENT),
        snapshot=service.build_snapshot(evaluation_mode=EvaluationMode.CURRENT),
        glossary=load_report_glossary(),
        generated_on=today,
    )

    assert data["meta"]["as_of"] == today.isoformat()
    assert data["meta"]["stale"] is False
    assert data["prev"]["date"] == earlier.isoformat()
    assert [row["label"] for row in data["prev"]["rows"][:2]] == ["增长得分", "通胀压力得分"]
    assert len(data["cur"]["pillars"]) == 9
    assert {item["d"] for item in data["hist"]} >= {"2020-04-30", earlier.isoformat()}
    assert data["hist_meta"] == {"n_eod": 2}
    growth_total = sum(item["v"] for item in data["cur"]["gd"])
    assert growth_total == pytest.approx(data["cur"]["g"], abs=1e-3)
    for key in ("verdict", "change_answer", "growth_answer", "infl_answer", "rel_answer"):
        assert data["text"][key]
    assert "0000000" not in "".join(str(value) for value in data["text"].values())

    stale = build_report_data(
        postgres_session,
        loaded=service.loader.load(evaluation_mode=EvaluationMode.CURRENT),
        snapshot=service.build_snapshot(evaluation_mode=EvaluationMode.CURRENT),
        glossary=load_report_glossary(),
        generated_on=today + timedelta(days=30),
    )
    assert stale["meta"]["stale"] is True
    assert stale["meta"]["stale_days"] == 30
    assert "<!doctype html>" in render_visual_report(data)


def test_report_shows_the_newest_published_number_and_names_a_trailing_primary(
    postgres_session: Session,
) -> None:
    today = date.today()
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    weeks = [today - timedelta(days=7 * offset) for offset in range(12, 0, -1)]

    def observation(source: str, when: date, value: int) -> RawObservation:
        return RawObservation(
            indicator_id="US_INITIAL_CLAIMS",
            observation_date=when,
            value=Decimal(value),
            source=source,
            retrieved_at=datetime.now(UTC),
            raw_payload_hash=source[0] * 64,
        )

    postgres_session.add_all(
        [observation("dol", when, 200_000) for when in weeks[:-3]]
        + [observation("fred", when, 200_000) for when in weeks[:-3]]
        + [observation("fred", when, 240_000) for when in weeks[-3:]]
    )
    postgres_session.flush()
    persist_current_context(postgres_session, asyncio.run(_context()))

    service = MacroExplanationService(postgres_session, load_explanation_authority())
    data = build_report_data(
        postgres_session,
        loaded=service.loader.load(evaluation_mode=EvaluationMode.CURRENT),
        snapshot=service.build_snapshot(evaluation_mode=EvaluationMode.CURRENT),
        glossary=load_report_glossary(),
        generated_on=today,
    )

    assert [item["ind"] for item in data["lags"]] == ["US_INITIAL_CLAIMS"]
    lag = data["lags"][0]
    assert lag["primary_last"] == weeks[-4].isoformat()
    assert lag["fallback_last"] == weeks[-1].isoformat()
    card = next(item for item in data["series"]["sm"] if item["name"] == "首次申领失业金")
    assert card["pts"][-1] == [weeks[-1].isoformat(), 230.0]
    assert card["weekly"] is True
    assert "FRED" in card["src"]
    assert all(not item["src"] for item in data["series"]["sm"] if item is not card)
    assert data["stats"][3]["flag"] == "留意"
    assert any("首次申领失业金" in line and "FRED" in line for line in data["text"]["rel_points"])
