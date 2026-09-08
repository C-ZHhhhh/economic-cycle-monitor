"""Agent explanation authority, snapshot, attribution, and safety tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import pytest
from sqlalchemy.orm import Session

from agents.macro.explanation_builder import MacroExplanationSnapshotBuilder
from agents.macro.explanation_cache import BoundedLRU, NarrativeCacheKey
from agents.macro.explanation_config import load_explanation_authority
from agents.macro.explanation_llm import (
    ExplanationOutputValidationError,
    MockStructuredExplanationClient,
    StructuredExplanationClient,
    validate_structured_narrative,
)
from agents.macro.explanation_loader import (
    PersistedExplanationContextLoader,
    persisted_context_hash,
    seal_persisted_context,
)
from agents.macro.explanation_report import render_deterministic_report
from agents.macro.explanation_schemas import (
    DependencyAvailability,
    DriverAxis,
    LoadedExplanationContext,
    MacroExplanationNarrative,
    NarrativeStatus,
    PersistedCadence,
    PersistedExplanationContext,
    RevisionStatus,
    StructuredExplanationRequest,
    StructuredExplanationResponse,
)
from agents.macro.explanation_service import MacroExplanationService
from app.composites.schemas import EvaluationMode
from app.composites.service import MacroStateEngine
from app.pillars.schemas import PillarRole
from app.pillars.service import PillarEngine
from app.regimes.calculations import subtract_calendar_months
from app.regimes.service import MacroRegimeEngine
from app.source_registry.schemas import ReadinessStatus, RuntimeHealth
from tests.unit.test_composite_service import FakePillarEngine
from tests.unit.test_pillar_service import PILLARS, FakeSignalEngine, _row
from tests.unit.test_regime_service import FakeMacroStateEngine

AUTHORITY = load_explanation_authority()


def _readiness() -> ReadinessStatus:
    return ReadinessStatus(
        registry_policy_version=AUTHORITY.required_source_registry_version,
        registry_hash=AUTHORITY.required_source_registry_hash,
        current_freshness_version=AUTHORITY.required_current_freshness_version,
        current_freshness_hash=AUTHORITY.required_current_freshness_hash,
        current_data_ready=True,
        pit_contract_ready=True,
        pit_runtime_ready=True,
        pit_runtime_health=RuntimeHealth.HEALTHY,
        pit_backtest_ready=True,
    )


async def _context(
    as_of: date | None = None,
    *,
    cadence: PersistedCadence = PersistedCadence.RELEASE_EVENT_EOD,
    offset: Decimal = Decimal(0),
) -> PersistedExplanationContext:
    evaluation_date = as_of or date.today()
    lag_date = subtract_calendar_months(evaluation_date, 3)
    primary_ids = [
        signal_id for pillar in PILLARS.pillars for signal_id in pillar.primary_signal_ids
    ]
    current_signals = [
        _row(
            signal_id,
            Decimal(index % 5 - 2) / Decimal(2) + offset,
            observation_date=evaluation_date,
            as_of=as_of,
        )
        for index, signal_id in enumerate(primary_ids)
    ]
    lag_signals = [
        _row(
            signal_id,
            Decimal(index % 5 - 2) / Decimal(2),
            observation_date=lag_date,
            as_of=lag_date,
        )
        for index, signal_id in enumerate(primary_ids)
    ]
    current_pillars = await PillarEngine(
        Session(), signal_engine=FakeSignalEngine(current_signals)
    ).compute_all_pillars(as_of)
    lag_pillars = await PillarEngine(
        Session(), signal_engine=FakeSignalEngine(lag_signals)
    ).compute_all_pillars(lag_date)
    current_state = await MacroStateEngine(
        Session(), pillar_engine=FakePillarEngine(current_pillars)
    ).compute_macro_state(as_of)
    lag_state = await MacroStateEngine(
        Session(), pillar_engine=FakePillarEngine(lag_pillars)
    ).compute_macro_state(lag_date)
    regime = await MacroRegimeEngine(
        Session(),
        macro_state_engine=FakeMacroStateEngine({as_of: current_state, lag_date: lag_state}),
    ).compute_regime(as_of)
    return seal_persisted_context(
        cadence=cadence,
        release_event_id=f"fixture:{evaluation_date}",
        regime=regime,
        macro_state=current_state,
        lagged_macro_state=lag_state,
        pillars=tuple(current_pillars),
        signals=tuple(current_signals),
        readiness=_readiness() if as_of is None else None,
        health=(),
        materialized_at=datetime.combine(evaluation_date, datetime.min.time(), tzinfo=UTC),
    )


def _loaded(
    current: PersistedExplanationContext,
    previous_release: PersistedExplanationContext | None = None,
    previous_month: PersistedExplanationContext | None = None,
) -> LoadedExplanationContext:
    return LoadedExplanationContext(
        status=DependencyAvailability.AVAILABLE,
        current=current,
        previous_release_eod_status=(
            DependencyAvailability.AVAILABLE
            if previous_release
            else DependencyAvailability.COMPARISON_NOT_AVAILABLE
        ),
        previous_release_eod=previous_release,
        previous_month_end_status=(
            DependencyAvailability.AVAILABLE
            if previous_month
            else DependencyAvailability.COMPARISON_NOT_AVAILABLE
        ),
        previous_month_end=previous_month,
        trajectory_context_status=DependencyAvailability.AVAILABLE,
    )


@pytest.mark.asyncio
async def test_authority_and_snapshot_are_deterministic_and_bound() -> None:
    context = await _context()
    builder = MacroExplanationSnapshotBuilder(AUTHORITY)
    first = builder.build(_loaded(context))
    second = builder.build(_loaded(context))
    assert AUTHORITY.agent_version == "agent-explanation-v1"
    assert first.snapshot_calculation_hash == second.snapshot_calculation_hash
    assert first.snapshot_spec_hash == AUTHORITY.specification_hash
    assert len(first.pillars) == 9
    assert [item.pillar_id for item in first.pillars] == sorted(
        item.pillar_id for item in first.pillars
    )
    assert first.authorities.source_registry_hash == AUTHORITY.required_source_registry_hash
    assert first.authorities.regime_spec_hash == AUTHORITY.required_regime_spec_hash


@pytest.mark.asyncio
async def test_signal_authority_drift_fails_closed() -> None:
    context = await _context()
    signals = list(context.signals)
    signals[0] = signals[0].model_copy(update={"normalization_spec_hash": "wrong"})
    drifted = context.model_copy(update={"signals": tuple(signals)})
    with pytest.raises(RuntimeError, match="SIGNAL_AUTHORITY_MISMATCH"):
        MacroExplanationSnapshotBuilder(AUTHORITY).build(_loaded(drifted))


@pytest.mark.asyncio
async def test_driver_decomposition_reuses_production_weights() -> None:
    snapshot = MacroExplanationSnapshotBuilder(AUTHORITY).build(_loaded(await _context()))
    with localcontext() as context:
        context.prec = 50
        growth_total = sum(
            (item.attributed_impact for item in snapshot.drivers.full_growth), Decimal(0)
        )
        inflation_total = sum(
            (item.attributed_impact for item in snapshot.drivers.full_inflation), Decimal(0)
        )
        assert abs(growth_total - snapshot.growth_strength) < Decimal("1e-28")  # type: ignore[operator]
        assert abs(inflation_total - snapshot.inflation_pressure) < Decimal("1e-28")  # type: ignore[operator]
    assert all(item.axis is DriverAxis.GROWTH for item in snapshot.drivers.full_growth)
    assert all(
        next(p for p in snapshot.pillars if p.pillar_id == item.pillar_id).pillar_role
        is PillarRole.CORE
        for item in snapshot.drivers.full_growth
    )
    assert len(snapshot.drivers.top_positive_growth) <= 3
    assert len(snapshot.drivers.top_negative_growth) <= 3
    assert len(snapshot.drivers.top_inflationary) <= 3
    assert len(snapshot.drivers.top_disinflationary) <= 3


@pytest.mark.asyncio
async def test_current_contract_separates_readiness_coverage_and_revision() -> None:
    snapshot = MacroExplanationSnapshotBuilder(AUTHORITY).build(_loaded(await _context()))
    assert snapshot.readiness is not None and snapshot.readiness.current_data_ready
    assert snapshot.minimum_axis_confidence <= Decimal(1)
    assert snapshot.revision.status is RevisionStatus.NOT_AVAILABLE_FOR_CURRENT_MODE
    assert snapshot.authorities.revision_provenance_hash is None


@pytest.mark.asyncio
async def test_pit_contract_excludes_current_freshness() -> None:
    context = await _context(date(2022, 6, 30))
    snapshot = MacroExplanationSnapshotBuilder(AUTHORITY).build(_loaded(context))
    assert snapshot.readiness is None
    assert not snapshot.current_health
    assert snapshot.authorities.current_freshness_hash is None
    assert snapshot.revision.status in {
        RevisionStatus.AVAILABLE,
        RevisionStatus.NO_REVISION_EVIDENCE,
    }


@pytest.mark.asyncio
async def test_comparisons_use_strict_persisted_cadences() -> None:
    previous_release = await _context(date(2022, 5, 20), offset=Decimal("-.1"))
    previous_month = await _context(
        date(2022, 5, 31),
        cadence=PersistedCadence.MONTH_END_EOD,
        offset=Decimal("-.2"),
    )
    current = await _context(date(2022, 6, 30), offset=Decimal(".2"))
    snapshot = MacroExplanationSnapshotBuilder(AUTHORITY).build(
        _loaded(current, previous_release, previous_month)
    )
    assert snapshot.previous_release_eod.previous_evaluation_as_of == date(2022, 5, 20)
    assert snapshot.previous_month_end.previous_evaluation_as_of == date(2022, 5, 31)
    assert snapshot.previous_release_eod.changes
    assert snapshot.previous_month_end.changes


@pytest.mark.asyncio
async def test_missing_comparisons_are_typed_not_zero() -> None:
    snapshot = MacroExplanationSnapshotBuilder(AUTHORITY).build(_loaded(await _context()))
    assert snapshot.previous_release_eod.status is DependencyAvailability.COMPARISON_NOT_AVAILABLE
    assert not snapshot.previous_release_eod.changes


@pytest.mark.asyncio
async def test_deterministic_report_is_complete_and_stable() -> None:
    snapshot = MacroExplanationSnapshotBuilder(AUTHORITY).build(_loaded(await _context()))
    first = render_deterministic_report(snapshot)
    second = render_deterministic_report(snapshot)
    assert first.report_hash == second.report_hash
    assert "not probabilities" in first.confidence_note
    assert "CURRENT_DATA_READY=true" in first.freshness_note
    assert snapshot.revision.note in first.markdown


@pytest.mark.asyncio
async def test_mock_narrative_validates_without_real_network() -> None:
    snapshot = MacroExplanationSnapshotBuilder(AUTHORITY).build(_loaded(await _context()))
    client = MockStructuredExplanationClient()
    request = StructuredExplanationRequest(
        prompt_version=AUTHORITY.prompt_version,
        prompt_hash=AUTHORITY.prompt_hash,
        system_prompt=AUTHORITY.prompt_text,
        snapshot=snapshot.model_dump(mode="json"),
    )
    response = await client.generate_structured(request)
    narrative = validate_structured_narrative(response, snapshot)
    assert narrative.narrative_status is NarrativeStatus.VALIDATED_LLM


@pytest.mark.parametrize(
    ("replacement", "error"),
    [
        ({"evidence_refs": ["unknown"]}, "UNKNOWN_EVIDENCE_REFERENCE"),
        ({"text": "The unsupported value is 999."}, "UNSUPPORTED_LLM_NUMERIC_CLAIM"),
        ({"text": "This is investment advice."}, "PROHIBITED_MARKET"),
    ],
)
@pytest.mark.asyncio
async def test_narrative_rejects_unsupported_claims(
    replacement: dict[str, object], error: str
) -> None:
    snapshot = MacroExplanationSnapshotBuilder(AUTHORITY).build(_loaded(await _context()))
    response = await MockStructuredExplanationClient().generate_structured(
        StructuredExplanationRequest(
            prompt_version=AUTHORITY.prompt_version,
            prompt_hash=AUTHORITY.prompt_hash,
            system_prompt=AUTHORITY.prompt_text,
            snapshot=snapshot.model_dump(mode="json"),
        )
    )
    payload = response.output.copy()
    claim = dict(payload["claims"][0])
    claim.update(replacement)
    payload["claims"] = [claim]
    with pytest.raises(ExplanationOutputValidationError, match=error):
        validate_structured_narrative(response.model_copy(update={"output": payload}), snapshot)


@pytest.mark.asyncio
async def test_malformed_narrative_is_rejected() -> None:
    snapshot = MacroExplanationSnapshotBuilder(AUTHORITY).build(_loaded(await _context()))
    response = StructuredExplanationResponse(output={}, provider="fixture")
    with pytest.raises(ExplanationOutputValidationError, match="MALFORMED"):
        validate_structured_narrative(response, snapshot)


class FailingClient(StructuredExplanationClient):
    provider = "failing"
    model = "fixture"
    generation_settings = {"temperature": "0"}

    async def generate_structured(
        self, request: StructuredExplanationRequest
    ) -> StructuredExplanationResponse:
        del request
        raise TimeoutError("fixture")


class StaticLoader(PersistedExplanationContextLoader):
    def __init__(self, loaded: LoadedExplanationContext) -> None:
        self.loaded = loaded

    def load(
        self,
        *,
        evaluation_mode: EvaluationMode,
        evaluation_as_of: date | None = None,
    ) -> LoadedExplanationContext:
        del evaluation_mode, evaluation_as_of
        return self.loaded


class ContextListLoader(PersistedExplanationContextLoader):
    def __init__(self, contexts: list[PersistedExplanationContext]) -> None:
        self.contexts = contexts

    def _contexts(self) -> list[PersistedExplanationContext]:
        return list(self.contexts)


@pytest.mark.asyncio
async def test_loader_selects_previous_release_and_month_end_strictly_before_current() -> None:
    old_month = await _context(date(2022, 4, 30), cadence=PersistedCadence.MONTH_END_EOD)
    release = await _context(date(2022, 5, 20))
    month = await _context(date(2022, 5, 31), cadence=PersistedCadence.MONTH_END_EOD)
    current = await _context(date(2022, 6, 30))
    loaded = ContextListLoader([current, old_month, release, month]).load(
        evaluation_mode=EvaluationMode.PIT_DATE_EOD,
        evaluation_as_of=date(2022, 6, 30),
    )
    assert loaded.previous_release_eod is not None
    assert loaded.previous_release_eod.evaluation_as_of == date(2022, 5, 20)
    assert loaded.previous_month_end is not None
    assert loaded.previous_month_end.evaluation_as_of == date(2022, 5, 31)


@pytest.mark.asyncio
async def test_llm_failure_returns_deterministic_only_without_provider_fallback() -> None:
    context = await _context()
    service = MacroExplanationService(Session(), AUTHORITY, loader=StaticLoader(_loaded(context)))
    snapshot = service.build_snapshot(evaluation_mode=EvaluationMode.CURRENT)
    result = await service.explain(snapshot, client=FailingClient())
    assert result.narrative is None
    assert result.narrative_status is NarrativeStatus.DETERMINISTIC_ONLY
    assert result.llm_error == "TimeoutError"


@pytest.mark.asyncio
async def test_explanation_path_never_invokes_upstream_compute_or_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _context()

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("implicit I/O or materialization")

    monkeypatch.setattr("app.regimes.service.compute_macro_regime", forbidden)
    monkeypatch.setattr("app.source_registry.pit.get_effective_observations_on_demand", forbidden)
    service = MacroExplanationService(Session(), AUTHORITY, loader=StaticLoader(_loaded(context)))
    snapshot = service.build_snapshot(evaluation_mode=EvaluationMode.CURRENT)
    report = render_deterministic_report(snapshot)
    assert snapshot.snapshot_calculation_hash
    assert report.report_hash


@pytest.mark.asyncio
async def test_context_hash_detects_mutation() -> None:
    context = (await _context()).model_copy(update={"persisted_context_hash": "wrong"})
    assert persisted_context_hash(context) != "wrong"


def test_cache_is_bounded_and_keys_isolate_provider_and_mode() -> None:
    cache: BoundedLRU[MacroExplanationNarrative] = BoundedLRU(max_entries=1)
    narrative = MacroExplanationNarrative(
        summary="summary",
        what_changed=(),
        primary_drivers=(),
        contextual_signals=(),
        confidence_note="confidence",
        freshness_note="freshness",
        revision_note="revision",
        warnings=(),
        narrative_status=NarrativeStatus.DETERMINISTIC_ONLY,
        claims=(),
    )
    first = NarrativeCacheKey("a", "b", "c", "mock", "one", "d")
    second = NarrativeCacheKey("a", "b", "c", "mock", "two", "d")
    cache.put(first, narrative)
    cache.put(second, narrative)
    assert cache.get(first) is None
    assert cache.get(second) is not None
    assert len(cache) == 1


def test_legacy_macro_cycle_contract_remains_unchanged() -> None:
    from agents.macro.schemas import MacroCycleResult

    schema = MacroCycleResult.model_json_schema()
    assert "regime_probabilities" in schema["properties"]
    assert "nowcast" in schema["properties"]
    assert "snapshot_calculation_hash" not in schema["properties"]


def test_prompt_is_versioned_and_forbids_recomputation() -> None:
    assert AUTHORITY.prompt_version == "agent-explanation-prompt-v1"
    assert (
        AUTHORITY.prompt_hash == hashlib.sha256(AUTHORITY.prompt_text.encode("utf-8")).hexdigest()
    )
    assert "Do not recompute" in AUTHORITY.prompt_text
    assert "investment advice" in AUTHORITY.prompt_text
