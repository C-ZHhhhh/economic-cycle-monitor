"""Provider-neutral structured LLM boundary and strict output validation."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable

from pydantic import ValidationError

from agents.macro.explanation_schemas import (
    ExplanationClaim,
    MacroExplanationNarrative,
    MacroExplanationSnapshot,
    NarrativeStatus,
    StructuredExplanationRequest,
    StructuredExplanationResponse,
)

NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9_])")
PROHIBITED_LANGUAGE = (
    "buy",
    "sell",
    "trading signal",
    "investment advice",
    "market prediction",
    "recession probability",
    "probability of recession",
)


class StructuredExplanationClient(ABC):
    """Generate one structured narrative without exposing tools or source access."""

    provider: str
    model: str | None
    generation_settings: dict[str, object]

    @abstractmethod
    async def generate_structured(
        self, request: StructuredExplanationRequest
    ) -> StructuredExplanationResponse:
        """Return a provider result conforming to the requested JSON schema."""


class MockStructuredExplanationClient(StructuredExplanationClient):
    """Zero-network provider used by tests and the default CLI mode."""

    provider = "mock"
    model = "mock-structured-v1"
    generation_settings = {"temperature": "0"}

    async def generate_structured(
        self, request: StructuredExplanationRequest
    ) -> StructuredExplanationResponse:
        """Return a fixed evidence-bound narrative without network access."""
        evidence = request.snapshot.get("evidence_registry", [])
        first_id = str(evidence[0]["evidence_id"]) if evidence else ""
        output = MacroExplanationNarrative(
            summary="The deterministic snapshot is available for explanation.",
            what_changed=("Only persisted comparison evidence was considered.",),
            primary_drivers=("Driver ordering follows the deterministic snapshot.",),
            contextual_signals=("Context remains separate from core growth attribution.",),
            confidence_note="Confidence describes data coverage, not probability.",
            freshness_note="Freshness follows the supplied mode-specific evidence.",
            revision_note="Revision language follows the supplied revision status.",
            warnings=(),
            narrative_status=NarrativeStatus.VALIDATED_LLM,
            claims=(
                ExplanationClaim(
                    claim_id="claim-summary",
                    text="The explanation uses the frozen deterministic result.",
                    evidence_refs=(first_id,),
                ),
            ),
        )
        return StructuredExplanationResponse(
            output=output.model_dump(mode="json"),
            provider=self.provider,
            model=self.model,
            generation_settings=self.generation_settings,
        )


class ExplanationOutputValidationError(ValueError):
    """A provider artifact introduced unsupported content or evidence."""


def _narrative_texts(narrative: MacroExplanationNarrative) -> Iterable[str]:
    yield narrative.summary
    yield from narrative.what_changed
    yield from narrative.primary_drivers
    yield from narrative.contextual_signals
    yield narrative.confidence_note
    yield narrative.freshness_note
    yield narrative.revision_note
    yield from narrative.warnings
    yield from (claim.text for claim in narrative.claims)


def validate_structured_narrative(
    response: StructuredExplanationResponse,
    snapshot: MacroExplanationSnapshot,
) -> MacroExplanationNarrative:
    """Validate schema, evidence, numeric safety, and prohibited semantics."""
    try:
        narrative = MacroExplanationNarrative.model_validate(response.output)
    except ValidationError as exc:
        raise ExplanationOutputValidationError("MALFORMED_LLM_OUTPUT") from exc
    if narrative.narrative_status is not NarrativeStatus.VALIDATED_LLM:
        raise ExplanationOutputValidationError("INVALID_LLM_NARRATIVE_STATUS")
    evidence = {item.evidence_id: item for item in snapshot.evidence_registry}
    for claim in narrative.claims:
        unknown = set(claim.evidence_refs) - set(evidence)
        if unknown:
            raise ExplanationOutputValidationError("UNKNOWN_EVIDENCE_REFERENCE")
    allowed_numbers = {
        item.formatted_value
        for item in snapshot.evidence_registry
        if item.formatted_value is not None
    }
    for text in _narrative_texts(narrative):
        lowered = text.casefold()
        if any(term in lowered for term in PROHIBITED_LANGUAGE):
            raise ExplanationOutputValidationError("PROHIBITED_MARKET_OR_INVESTMENT_LANGUAGE")
        for number in NUMBER_PATTERN.findall(text):
            if number not in allowed_numbers:
                raise ExplanationOutputValidationError("UNSUPPORTED_LLM_NUMERIC_CLAIM")
    return narrative
