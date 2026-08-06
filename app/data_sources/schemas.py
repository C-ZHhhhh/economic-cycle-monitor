"""Shared schemas for future economic data-source adapters."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class SourceObservation(BaseModel):
    """Normalized observation returned by a future data source."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    observation_date: date
    value: Decimal
    source: str
    retrieved_at: datetime
    raw_payload_hash: str
