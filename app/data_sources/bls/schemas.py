"""Typed BLS API response and request-audit schemas."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BlsFootnote(BaseModel):
    """Optional BLS code and text attached to one data point."""

    model_config = ConfigDict(extra="ignore")

    code: str | None = None
    text: str | None = None


class BlsDataPoint(BaseModel):
    """One raw BLS period/value record."""

    model_config = ConfigDict(extra="ignore")

    year: int
    period: str
    periodName: str
    latest: str | bool | None = None
    value: Decimal | None
    footnotes: list[BlsFootnote] = Field(default_factory=list)

    @field_validator("value", mode="before")
    @classmethod
    def parse_missing_value(cls, value: object) -> object:
        """Preserve missing values instead of converting them to zero."""
        return None if value in {None, "", ".", "-"} else value


class BlsSeries(BaseModel):
    """One requested BLS series and its descending source data."""

    model_config = ConfigDict(extra="ignore")

    seriesID: str
    catalog: dict[str, Any] | None = None
    data: list[BlsDataPoint] = Field(default_factory=list)


class BlsResults(BaseModel):
    """BLS response results wrapper."""

    model_config = ConfigDict(extra="ignore")

    series: list[BlsSeries] = Field(default_factory=list)


class BlsApiResponse(BaseModel):
    """BLS business envelope returned even for some HTTP-success failures."""

    model_config = ConfigDict(extra="ignore")

    status: str
    responseTime: int | None = None
    message: list[str] = Field(default_factory=list)
    Results: BlsResults


class BlsPayload(BaseModel):
    """Secret-free request evidence passed to synchronization services."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    endpoint: str
    series_ids: list[str]
    sanitized_parameters: dict[str, Any]
    body: dict[str, Any]
    started_at: datetime
    finished_at: datetime
    status_code: int
    attempt_count: int
    response_hash: str
    response_path: Path | None
    from_cache: bool
    business_messages: list[str]
