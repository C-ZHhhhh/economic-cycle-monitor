"""Deterministic no-network LLM implementation for Stage 1."""

from app.llm.base import BaseLLMClient
from app.llm.schemas import LLMRequest, LLMResponse


class MockLLMClient(BaseLLMClient):
    """Return fixed content without performing an external request."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Return a deterministic response suitable for tests."""
        del request
        return LLMResponse(content="mock", provider="mock", model=None)
