"""Typed FRED response and normalized domain schemas."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser
from pydantic import BaseModel, ConfigDict, Field, field_validator


class FredSeriesMetadata(BaseModel):
    """Relevant fields returned by ``fred/series``."""

    model_config = ConfigDict(extra="ignore")

    id: str
    realtime_start: date
    realtime_end: date
    title: str
    observation_start: date
    observation_end: date
    frequency: str
    frequency_short: str
    units: str
    units_short: str
    seasonal_adjustment: str
    seasonal_adjustment_short: str
    last_updated: datetime
    popularity: int
    notes: str | None = None

    @field_validator("last_updated", mode="before")
    @classmethod
    def parse_fred_datetime(cls, value: object) -> object:
        """Accept FRED's hour-only UTC offsets (for example ``-06``)."""
        return date_parser.parse(value) if isinstance(value, str) else value


class FredObservation(BaseModel):
    """One raw FRED/ALFRED observation, including its real-time window."""

    model_config = ConfigDict(extra="ignore")

    realtime_start: date
    realtime_end: date
    date: date
    value: Decimal | None

    @field_validator("value", mode="before")
    @classmethod
    def parse_missing_value(cls, value: object) -> object:
        return None if value in {".", "", None} else value


class FredRelease(BaseModel):
    """Release metadata associated with a FRED series."""

    model_config = ConfigDict(extra="ignore")

    id: int
    realtime_start: date
    realtime_end: date
    name: str
    press_release: bool = False
    link: str | None = None


class FredPayload(BaseModel):
    """Successful request evidence passed to parsers and audit persistence."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    endpoint: str
    series_id: str | None
    sanitized_parameters: dict[str, str]
    body: dict[str, Any]
    started_at: datetime
    finished_at: datetime
    status_code: int
    attempt_count: int
    response_hash: str
    response_path: Path | None
    from_cache: bool


class FredPage(BaseModel):
    """Parsed pagination fields shared by collection endpoints."""

    model_config = ConfigDict(extra="allow")

    count: int = 0
    offset: int = 0
    limit: int = Field(default=1000, gt=0)
