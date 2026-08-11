"""Typed Census EITS mappings, rows, and request evidence."""

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MappingType = Literal["DIRECT", "DERIVED", "SUPPORTING_COMPONENT"]


class CensusMappingIdentity(BaseModel):
    """Complete candidate identity for one multidimensional EITS series."""

    model_config = ConfigDict(extra="forbid")

    mapping_type: MappingType
    program: str
    dataset: str
    category_code: str
    data_type_code: str
    seasonally_adjusted: bool
    seasonally_adj: str | None = None
    geography: str
    geo_level_code: str | None = None
    geography_query: str
    identity_fields: list[str] = Field(default_factory=list)
    first_available_period: str
    source_unit_code: str
    scale: int

    @model_validator(mode="after")
    def require_candidate_dimensions(self) -> "CensusMappingIdentity":
        """Reject incomplete static dimensions while allowing live-only wire values."""
        if not all(
            (
                self.program,
                self.dataset,
                self.category_code,
                self.data_type_code,
                self.geography,
                self.geography_query,
                self.first_available_period,
                self.source_unit_code,
            )
        ):
            raise ValueError("Census candidate identity requires every documented dimension")
        if self.scale < 1:
            raise ValueError("Census source scale must be positive")
        return self


class CensusRow(BaseModel):
    """One raw EITS row keyed by its returned header names."""

    model_config = ConfigDict(extra="allow")

    cell_value: str | None = None
    category_code: str
    data_type_code: str
    seasonally_adj: str
    geo_level_code: str
    time: str
    time_slot_id: str | None = None
    time_slot_date: str | None = None
    time_slot_name: str | None = None
    program_code: str | None = None
    error_data: str | None = None

    def returned_dimensions(self) -> dict[str, Any]:
        """Return all official row dimensions without discarding unknown fields."""
        return self.model_dump(exclude_none=True)


class CensusPayload(BaseModel):
    """Credential-free evidence for one Census request."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    endpoint: str
    sanitized_parameters: dict[str, Any]
    body: Any
    started_at: datetime
    finished_at: datetime
    status_code: int
    attempt_count: int
    response_hash: str
    response_path: Path | None
    from_cache: bool


class CensusMetadataValidation(BaseModel):
    """Live identity evidence that never promotes a pending mapping automatically."""

    status: Literal["pending_review", "invalid", "unavailable"] = "pending_review"
    identity_confirmed: bool = False
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    returned_seasonal_values: list[str] = Field(default_factory=list)
    returned_geography_values: list[str] = Field(default_factory=list)
