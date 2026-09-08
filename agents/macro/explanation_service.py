"""No-I/O explanation orchestration with deterministic LLM fallback."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from agents.macro.explanation_builder import MacroExplanationSnapshotBuilder
from agents.macro.explanation_cache import BoundedLRU, NarrativeCacheKey
from agents.macro.explanation_config import ExplanationAuthority, hash_payload
from agents.macro.explanation_llm import (
    StructuredExplanationClient,
    validate_structured_narrative,
)
from agents.macro.explanation_loader import PersistedExplanationContextLoader
from agents.macro.explanation_report import render_deterministic_report
from agents.macro.explanation_schemas import (
    ExplanationExecutionResult,
    MacroExplanationNarrative,
    MacroExplanationSnapshot,
    NarrativeStatus,
    StructuredExplanationRequest,
)
from app.composites.schemas import EvaluationMode


class MacroExplanationService:
    """Build and explain only locally persisted frozen-model results."""

    def __init__(
        self,
        session: Session,
        authority: ExplanationAuthority,
        *,
        loader: PersistedExplanationContextLoader | None = None,
        snapshot_cache: BoundedLRU[MacroExplanationSnapshot] | None = None,
        narrative_cache: BoundedLRU[MacroExplanationNarrative] | None = None,
    ) -> None:
        self.loader = loader or PersistedExplanationContextLoader(session)
        self.authority = authority
        self.builder = MacroExplanationSnapshotBuilder(authority)
        self.snapshot_cache = snapshot_cache or BoundedLRU(max_entries=64)
        self.narrative_cache = narrative_cache or BoundedLRU(max_entries=64)

    def build_snapshot(
        self,
        *,
        evaluation_mode: EvaluationMode,
        evaluation_as_of: date | None = None,
    ) -> MacroExplanationSnapshot:
        """Load persisted context and build a pure snapshot without upstream computation."""
        loaded = self.loader.load(
            evaluation_mode=evaluation_mode,
            evaluation_as_of=evaluation_as_of,
        )
        loaded_identity = hash_payload(loaded.model_dump(mode="json"))
        cache_key = (
            evaluation_mode.value,
            evaluation_as_of.isoformat() if evaluation_as_of else "LATEST",
            self.authority.specification_hash,
            loaded_identity,
        )
        cached = self.snapshot_cache.get(cache_key)
        if cached is not None:
            return cached
        snapshot = self.builder.build(loaded)
        self.snapshot_cache.put(cache_key, snapshot)
        return snapshot.model_copy(deep=True)

    async def explain(
        self,
        snapshot: MacroExplanationSnapshot,
        *,
        client: StructuredExplanationClient | None = None,
    ) -> ExplanationExecutionResult:
        """Return validated structured prose or the official deterministic fallback."""
        report = render_deterministic_report(snapshot)
        if client is None:
            return ExplanationExecutionResult(
                snapshot=snapshot,
                deterministic_report=report,
                narrative_status=NarrativeStatus.DETERMINISTIC_ONLY,
                llm_error=None,
                prompt_version=self.authority.prompt_version,
                prompt_hash=self.authority.prompt_hash,
            )
        request = StructuredExplanationRequest(
            prompt_version=self.authority.prompt_version,
            prompt_hash=self.authority.prompt_hash,
            system_prompt=self.authority.prompt_text,
            snapshot=snapshot.model_dump(mode="json"),
        )
        request_settings = dict(client.generation_settings)
        key = NarrativeCacheKey(
            snapshot_calculation_hash=snapshot.snapshot_calculation_hash,
            prompt_hash=self.authority.prompt_hash,
            output_schema_version=request.output_schema_version,
            provider=client.provider,
            model=client.model,
            generation_settings_hash=hash_payload(request_settings),
        )
        cached = self.narrative_cache.get(key)
        if cached is not None:
            return ExplanationExecutionResult(
                snapshot=snapshot,
                deterministic_report=report,
                narrative=cached,
                narrative_status=NarrativeStatus.VALIDATED_LLM,
                provider=client.provider,
                model=client.model,
                generation_settings=request_settings,
                prompt_version=self.authority.prompt_version,
                prompt_hash=self.authority.prompt_hash,
            )
        try:
            response = await client.generate_structured(request)
            if response.provider != client.provider or response.model != client.model:
                raise ValueError("LLM_PROVIDER_IDENTITY_MISMATCH")
            if response.generation_settings != request_settings:
                raise ValueError("LLM_GENERATION_SETTINGS_MISMATCH")
            narrative = validate_structured_narrative(response, snapshot)
        except Exception as exc:  # provider boundary intentionally becomes typed fallback
            return ExplanationExecutionResult(
                snapshot=snapshot,
                deterministic_report=report,
                narrative_status=NarrativeStatus.DETERMINISTIC_ONLY,
                llm_error=type(exc).__name__,
                provider=client.provider,
                model=client.model,
                generation_settings=request_settings,
                prompt_version=self.authority.prompt_version,
                prompt_hash=self.authority.prompt_hash,
            )
        self.narrative_cache.put(key, narrative)
        return ExplanationExecutionResult(
            snapshot=snapshot,
            deterministic_report=report,
            narrative=narrative,
            narrative_status=NarrativeStatus.VALIDATED_LLM,
            provider=response.provider,
            model=response.model,
            generation_settings=response.generation_settings,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            prompt_version=self.authority.prompt_version,
            prompt_hash=self.authority.prompt_hash,
        )
