"""Typed DOL Weekly Claims identities and request evidence."""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DolMappingIdentity(BaseModel):
    """Strict candidate identity whose XML paths remain pending until Live Gate."""

    model_config = ConfigDict(extra="forbid")

    mapping_type: Literal["DIRECT"] = "DIRECT"
    report: Literal["r539cy"]
    scope: Literal["us"]
    program: Literal["regular_state"]
    series_concept: Literal["initial_claims", "insured_unemployment"]
    seasonal_adjustment: Literal["SA"]
    frequency: Literal["W"]
    unit: Literal["persons"]
    row_path: str | None = None
    scope_path: str | None = None
    program_path: str | None = None
    concept_path: str | None = None
    seasonal_path: str | None = None
    frequency_path: str | None = None
    unit_path: str | None = None
    observation_path: str | None = None
    value_path: str | None = None
    release_date_path: str | None = None
    expected_wire_values: dict[str, list[str]] = Field(default_factory=dict)
    first_available_period: str


class DolPayload(BaseModel):
    """Secret-free evidence for one National Weekly Claims XML request."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    endpoint: str
    sanitized_parameters: dict[str, str]
    body: bytes
    started_at: datetime
    finished_at: datetime
    status_code: int
    attempt_count: int
    response_hash: str
    response_path: Path | None
    from_cache: bool
    content_type: str | None = None
