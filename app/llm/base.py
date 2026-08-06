"""Abstract provider interface for future LLM integrations."""

from abc import ABC, abstractmethod

from app.llm.schemas import LLMRequest, LLMResponse


class BaseLLMClient(ABC):
    """Generate a response without binding agents to one provider SDK."""

    @abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate one typed response."""
