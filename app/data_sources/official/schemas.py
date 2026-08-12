"""Normalized official-source observations shared by provider adapters."""

from datetime import date as Date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OfficialObservation(BaseModel):
    """One normalized provider observation with source-specific audit details."""

    model_config = ConfigDict(extra="forbid")

    date: Date
    value: Decimal | None
    source_period: str
    release_date: Date | None = None
    release_date_status: str = "not_provided"
    is_preliminary: bool = False
    footnotes: list[dict[str, str | None]] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
