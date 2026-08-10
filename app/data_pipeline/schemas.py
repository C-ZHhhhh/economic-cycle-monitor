"""Data-pipeline command and persistence result schemas."""

from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ObservationWriteResult(BaseModel):
    """Counts from idempotently persisting one observation response."""

    inserted: int = 0
    revised: int = 0
    unchanged: int = 0
    missing: int = 0


class PipelineError(BaseModel):
    """Safe per-indicator failure summary."""

    indicator_id: str
    error_type: str
    message: str


class SyncSummary(BaseModel):
    """Public summary shared by metadata, backfill, sync, and vintage operations."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    operation: str
    status: Literal["succeeded", "partial", "failed"]
    indicator_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    skipped_count: int = 0
    inserted_count: int = 0
    revised_count: int = 0
    quality_event_count: int = 0
    request_count: int = 0
    cache_hit_count: int = 0
    errors: list[PipelineError] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)


class PipelineSelection(BaseModel):
    """Bounded selection shared by pipeline operations."""

    indicator_id: str | None = None
    module: str | None = None
    enabled_only: bool = True


class BackfillOptions(PipelineSelection):
    """Historical observation request options."""

    start: date
    end: date
    force: bool = False
    dry_run: bool = False


class SyncOptions(PipelineSelection):
    """Incremental request options."""

    since: date | None = None
    lookback_days: int | None = None
    dry_run: bool = False


class BlsBackfillOptions(PipelineSelection):
    """Bounded BLS historical request options expressed in source years."""

    start_year: int = Field(ge=1913, le=2100)
    end_year: int = Field(ge=1913, le=2100)
    dry_run: bool = False


class BlsSyncOptions(PipelineSelection):
    """Incremental BLS synchronization options."""

    dry_run: bool = False


class BeaBackfillOptions(PipelineSelection):
    """Bounded BEA NIPA history request options expressed in source years."""

    start_year: int = Field(ge=1929, le=2100)
    end_year: int = Field(ge=1929, le=2100)
    dry_run: bool = False


class BeaSyncOptions(PipelineSelection):
    """Incremental BEA synchronization options."""

    dry_run: bool = False


class SourceComparisonOptions(PipelineSelection):
    """Bounded source-to-source comparison options."""

    primary_source: str = "bls"
    secondary_source: str = "fred"
    start: date | None = None
    end: date | None = None
    page_size: int = Field(default=500, ge=50, le=2000)
    dry_run: bool = False
