"""Typed identities and evidence for Federal Reserve Board releases."""

from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FederalReserveMappingIdentity(BaseModel):
    """Complete candidate identity for one G.17 or G.19 series."""

    model_config = ConfigDict(extra="forbid")

    mapping_type: Literal["DIRECT"] = "DIRECT"
    release: Literal["G17", "G19"]
    canonical_series_id: str
    transport: Literal["official_text", "official_sdmx"]
    transport_series_code: str | None = None
    dataset: str
    group: str | None = None
    representation: Literal["index", "utilization", "level"]
    frequency: Literal["M"]
    seasonal_adjustment: Literal["SA"]
    source_unit: str
    expected_base_year: int | None = None
    data_path: str
    documentation_path: str | None = None
    metadata_path: str | None = None
    first_available_period: str


class FederalReservePayload(BaseModel):
    """Auditable evidence for one static official file request."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    endpoint: str
    body: bytes = Field(repr=False)
    started_at: datetime
    finished_at: datetime
    status_code: int
    attempt_count: int
    response_hash: str
    response_path: Path | None
    from_cache: bool
    content_type: str | None = None
    release_date: date | None = None
