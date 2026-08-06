"""Formal JSON contract emitted by the US macro-cycle agent."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class MacroCycleResult(BaseModel):
    """Stable structured interface consumed by future synthesis agents."""

    model_config = ConfigDict(extra="forbid")

    agent: Literal["us_macro_cycle_agent"] = "us_macro_cycle_agent"
    version: str = "1.0.0"
    run_id: UUID
    as_of: datetime
    country: Literal["US"] = "US"
    current_regime: str | None = None
    previous_regime: str | None = None
    regime_probabilities: dict[str, Probability] = Field(default_factory=dict)
    process_labels: list[str] = Field(default_factory=list)
    modules: dict[str, JsonValue] = Field(default_factory=dict)
    nowcast: dict[str, JsonValue] = Field(default_factory=dict)
    confidence: Probability | None = None
    supporting_evidence: list[dict[str, JsonValue]] = Field(default_factory=list)
    contradictory_evidence: list[dict[str, JsonValue]] = Field(default_factory=list)
    changed_signals: list[dict[str, JsonValue]] = Field(default_factory=list)
    main_risks: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    stale_data: list[str] = Field(default_factory=list)
    data_quality_status: Literal["not_run", "ok", "warning", "error"] = "not_run"
    scoring_version: str = "0.1.0"
    regime_engine_version: str = "0.1.0"
    llm_provider: str = "deepseek"
    llm_model: str | None = None
    report_paths: dict[str, str] = Field(default_factory=dict)

    @field_validator("as_of")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        """Reject ambiguous timestamps that lack timezone information."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_probability_total(self) -> "MacroCycleResult":
        """Require populated regime probabilities to form a distribution."""
        if self.regime_probabilities:
            total = sum(self.regime_probabilities.values())
            if abs(total - 1.0) > 1e-6:
                raise ValueError("regime_probabilities must sum to 1")
        return self
