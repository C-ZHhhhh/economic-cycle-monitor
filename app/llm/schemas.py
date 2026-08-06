"""Typed request and response models for future LLM providers."""

from pydantic import BaseModel, ConfigDict


class LLMRequest(BaseModel):
    """Provider-neutral text generation request."""

    model_config = ConfigDict(extra="forbid")
    prompt: str


class LLMResponse(BaseModel):
    """Provider-neutral text generation response."""

    model_config = ConfigDict(extra="forbid")
    content: str
    provider: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
