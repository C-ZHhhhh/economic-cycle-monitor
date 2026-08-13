"""Typed identities and evidence for Federal Reserve Board releases."""

from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    data_member: str | None = None
    wire_frequency: str | None = None
    data_representation: str | None = None
    wire_unit: str | None = None
    unit_multiplier: str | None = None
    holder: str | None = None
    credit_type: str | None = None
    currency: str | None = None
    expected_base_year: int | None = None
    data_path: str
    documentation_path: str | None = None
    metadata_path: str | None = None
    description_keywords: list[str]
    first_available_period: str

    @model_validator(mode="after")
    def require_complete_g19_wire_identity(self) -> "FederalReserveMappingIdentity":
        """Require every observed G.19 SeriesKey dimension in reviewed configuration."""
        if self.release != "G19":
            return self
        required = {
            "data_member": self.data_member,
            "wire_frequency": self.wire_frequency,
            "data_representation": self.data_representation,
            "wire_unit": self.wire_unit,
            "unit_multiplier": self.unit_multiplier,
            "holder": self.holder,
            "credit_type": self.credit_type,
            "currency": self.currency,
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise ValueError(f"G.19 wire identity is incomplete: {missing}")
        return self


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
