"""Complete ALFRED vintage-change discovery and immutable generation persistence."""

import hashlib
import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_sources.fred.client import FredClient
from app.data_sources.fred.schemas import FredPayload, FredVintageDatePage
from app.database.models import PitChangeIndexGeneration
from app.source_registry.materialization import PitMaterializationCatalog


class ChangeIndexStatus(StrEnum):
    """Immutable acquisition result for one index generation."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    MALFORMED = "MALFORMED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    UNKNOWN = "UNKNOWN"


class ChangeIndexError(RuntimeError):
    """Base typed change-index error."""


class ChangeIndexIncomplete(ChangeIndexError):
    """Provider pages did not close against their declared count."""


class ChangeIndexMalformed(ChangeIndexError):
    """Provider pages contradicted ordering or pagination rules."""


class ChangeIndexUnavailable(ChangeIndexError):
    """No authoritative generation can answer the requested date."""


class ChangeIndexStale(ChangeIndexError):
    """The target EOD lies beyond the proven complete-through watermark."""


class ChangeIndexOutsideCoverage(ChangeIndexError):
    """A complete index proves that no information set existed by the target EOD."""


class ChangeIndexNoVintage(ChangeIndexError):
    """A complete index proves the series has no vintage information sets."""


@dataclass(frozen=True)
class PageEvidence:
    """Secret-free completeness evidence for one provider page."""

    count: int
    offset: int
    limit: int
    retrieved: int
    response_hash: str
    from_cache: bool

    def as_payload(self) -> dict[str, Any]:
        """Return deterministic JSON-compatible evidence."""
        return {
            "count": self.count,
            "offset": self.offset,
            "limit": self.limit,
            "retrieved": self.retrieved,
            "response_hash": self.response_hash,
            "from_cache": self.from_cache,
        }


@dataclass(frozen=True)
class ChangeIndexDiscovery:
    """Complete, validated change dates plus their page evidence."""

    coverage_start: date
    coverage_end: date
    complete_through: date
    change_dates: tuple[date, ...]
    provider_count: int
    pages: tuple[PageEvidence, ...]
    generation_hash: str


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def validate_change_index_pages(
    pages: list[tuple[FredVintageDatePage, FredPayload]],
    *,
    coverage_start: date,
    coverage_end: date,
    source_registry_hash: str,
    materialization: PitMaterializationCatalog,
    indicator_id: str,
    provider_series_id: str,
) -> ChangeIndexDiscovery:
    """Prove page closure and return an immutable deterministic generation."""
    if not pages:
        raise ChangeIndexIncomplete("PIT_CHANGE_INDEX_INCOMPLETE:NO_PAGES")
    expected_count = pages[0][0].count
    expected_offset = 0
    dates: list[date] = []
    evidence: list[PageEvidence] = []
    for page, payload in pages:
        if page.count != expected_count:
            raise ChangeIndexMalformed("PIT_CHANGE_INDEX_MALFORMED:COUNT_CHANGED")
        if page.offset != expected_offset:
            raise ChangeIndexIncomplete("PIT_CHANGE_INDEX_INCOMPLETE:OFFSET_GAP")
        if page.limit != materialization.change_index.page_limit:
            raise ChangeIndexMalformed("PIT_CHANGE_INDEX_MALFORMED:LIMIT_DRIFT")
        if len(page.vintage_dates) > page.limit:
            raise ChangeIndexMalformed("PIT_CHANGE_INDEX_MALFORMED:PAGE_OVER_LIMIT")
        if any(
            left >= right
            for left, right in zip(page.vintage_dates, page.vintage_dates[1:], strict=False)
        ):
            raise ChangeIndexMalformed("PIT_CHANGE_INDEX_MALFORMED:PAGE_ORDER")
        evidence.append(
            PageEvidence(
                count=page.count,
                offset=page.offset,
                limit=page.limit,
                retrieved=len(page.vintage_dates),
                response_hash=payload.response_hash,
                from_cache=payload.from_cache,
            )
        )
        dates.extend(page.vintage_dates)
        expected_offset += len(page.vintage_dates)
        if expected_offset > expected_count:
            raise ChangeIndexMalformed("PIT_CHANGE_INDEX_MALFORMED:COUNT_OVERFLOW")
    if expected_offset != expected_count:
        raise ChangeIndexIncomplete("PIT_CHANGE_INDEX_INCOMPLETE:COUNT_NOT_CLOSED")
    if len(dates) != len(set(dates)):
        raise ChangeIndexMalformed("PIT_CHANGE_INDEX_MALFORMED:DUPLICATE_DATE")
    if dates != sorted(dates):
        raise ChangeIndexMalformed("PIT_CHANGE_INDEX_MALFORMED:GLOBAL_ORDER")
    if any(item < coverage_start or item > coverage_end for item in dates):
        raise ChangeIndexMalformed("PIT_CHANGE_INDEX_MALFORMED:DATE_OUTSIDE_COVERAGE")
    generation_payload = {
        "indicator_id": indicator_id,
        "provider": materialization.change_index.provider,
        "provider_series_id": provider_series_id,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "complete_through": coverage_end,
        "change_dates": dates,
        "provider_count": expected_count,
        "pagination": [item.as_payload() for item in evidence],
        "source_registry_hash": source_registry_hash,
        "pit_materialization_spec_version": materialization.pit_materialization_spec_version,
        "pit_materialization_spec_hash": materialization.specification_hash,
    }
    return ChangeIndexDiscovery(
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        complete_through=coverage_end,
        change_dates=tuple(dates),
        provider_count=expected_count,
        pages=tuple(evidence),
        generation_hash=_hash(generation_payload),
    )


async def discover_change_index(
    client: FredClient,
    *,
    indicator_id: str,
    provider_series_id: str,
    coverage_start: date,
    coverage_end: date,
    source_registry_hash: str,
    materialization: PitMaterializationCatalog,
) -> tuple[ChangeIndexDiscovery, list[FredPayload]]:
    """Fetch all pages sequentially and require an exact provider-count closure."""
    if coverage_end > materialization.closed_eod.latest_closed_date():
        raise ChangeIndexStale("PIT_CHANGE_INDEX_STALE:OPEN_EOD")
    pages: list[tuple[FredVintageDatePage, FredPayload]] = []
    payloads: list[FredPayload] = []
    offset = 0
    while True:
        page, payload = await client.get_vintage_date_page(
            provider_series_id,
            realtime_start=coverage_start,
            realtime_end=coverage_end,
            limit=materialization.change_index.page_limit,
            offset=offset,
        )
        pages.append((page, payload))
        payloads.append(payload)
        offset += len(page.vintage_dates)
        if offset >= page.count:
            break
        if not page.vintage_dates:
            raise ChangeIndexIncomplete("PIT_CHANGE_INDEX_INCOMPLETE:EMPTY_INTERMEDIATE_PAGE")
    return (
        validate_change_index_pages(
            pages,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            source_registry_hash=source_registry_hash,
            materialization=materialization,
            indicator_id=indicator_id,
            provider_series_id=provider_series_id,
        ),
        payloads,
    )


def latest_complete_generation(
    session: Session,
    *,
    indicator_id: str,
    provider_series_id: str,
    source_registry_hash: str,
    materialization: PitMaterializationCatalog,
) -> PitChangeIndexGeneration | None:
    """Return the newest compatible complete generation, regardless of its watermark."""
    return session.scalar(
        select(PitChangeIndexGeneration)
        .where(
            PitChangeIndexGeneration.indicator_id == indicator_id,
            PitChangeIndexGeneration.provider == materialization.change_index.provider,
            PitChangeIndexGeneration.provider_series_id == provider_series_id,
            PitChangeIndexGeneration.status == ChangeIndexStatus.COMPLETE.value,
            PitChangeIndexGeneration.source_registry_hash == source_registry_hash,
            PitChangeIndexGeneration.pit_materialization_spec_hash
            == materialization.specification_hash,
        )
        .order_by(PitChangeIndexGeneration.id.desc())
        .limit(1)
    )


async def refresh_change_index(
    client: FredClient,
    *,
    prior: PitChangeIndexGeneration,
    coverage_end: date,
    source_registry_hash: str,
    materialization: PitMaterializationCatalog,
) -> tuple[ChangeIndexDiscovery, list[FredPayload]]:
    """Extend one proven generation sequentially without mutating old evidence."""
    if prior.index_complete_through is None:
        raise ChangeIndexIncomplete("PIT_CHANGE_INDEX_INCOMPLETE:NO_WATERMARK")
    if coverage_end <= prior.index_complete_through:
        raise ValueError("incremental refresh must advance complete-through")
    refresh_start = prior.index_complete_through.fromordinal(
        prior.index_complete_through.toordinal() + 1
    )
    prior_dates = tuple(date.fromisoformat(item) for item in prior.change_dates_json)
    # The provider answers a range that holds no vintage date with a server error,
    # not an empty page, so "nothing new" is indistinguishable from an outage. Asking
    # from the last change date already proven makes every answer non-empty: that
    # date must come back, and whatever follows it is the increment.
    anchor = prior_dates[-1] if prior_dates else None
    delta, payloads = await discover_change_index(
        client,
        indicator_id=prior.indicator_id,
        provider_series_id=prior.provider_series_id,
        coverage_start=anchor if anchor is not None else prior.index_coverage_start,
        coverage_end=coverage_end,
        source_registry_hash=source_registry_hash,
        materialization=materialization,
    )
    if anchor is not None and (not delta.change_dates or delta.change_dates[0] != anchor):
        raise ChangeIndexMalformed("PIT_CHANGE_INDEX_MALFORMED:REFRESH_ANCHOR_MISSING")
    if any(item <= prior.index_complete_through for item in delta.change_dates if item != anchor):
        # A date inside the interval the prior generation proved complete would
        # rewrite old evidence; that needs a fresh full discovery, not a refresh.
        raise ChangeIndexMalformed("PIT_CHANGE_INDEX_MALFORMED:REFRESH_REWRITES_PROVEN_INTERVAL")
    combined_dates = (
        *prior_dates,
        *(item for item in delta.change_dates if item > prior.index_complete_through),
    )
    if tuple(sorted(set(combined_dates))) != combined_dates:
        raise ChangeIndexMalformed("PIT_CHANGE_INDEX_MALFORMED:REFRESH_OVERLAP")
    prior_pages = tuple(
        PageEvidence(
            count=int(item["count"]),
            offset=int(item["offset"]),
            limit=int(item["limit"]),
            retrieved=int(item["retrieved"]),
            response_hash=str(item["response_hash"]),
            from_cache=bool(item["from_cache"]),
        )
        for item in prior.pagination_evidence_json
    )
    combined_pages = (*prior_pages, *delta.pages)
    generation_payload = {
        "indicator_id": prior.indicator_id,
        "provider": materialization.change_index.provider,
        "provider_series_id": prior.provider_series_id,
        "coverage_start": prior.index_coverage_start,
        "coverage_end": coverage_end,
        "complete_through": coverage_end,
        "change_dates": combined_dates,
        "provider_count": len(combined_dates),
        "pagination": [item.as_payload() for item in combined_pages],
        "source_registry_hash": source_registry_hash,
        "pit_materialization_spec_version": materialization.pit_materialization_spec_version,
        "pit_materialization_spec_hash": materialization.specification_hash,
        "refresh_parent_generation_hash": prior.generation_hash,
        "refresh_interval": [refresh_start, coverage_end],
        "refresh_anchor": anchor,
    }
    return (
        ChangeIndexDiscovery(
            coverage_start=prior.index_coverage_start,
            coverage_end=coverage_end,
            complete_through=coverage_end,
            change_dates=combined_dates,
            provider_count=len(combined_dates),
            pages=combined_pages,
            generation_hash=_hash(generation_payload),
        ),
        payloads,
    )


def persist_change_index_generation(
    session: Session,
    *,
    indicator_id: str,
    provider_series_id: str,
    discovery: ChangeIndexDiscovery,
    source_registry_hash: str,
    materialization: PitMaterializationCatalog,
    discovered_at: datetime,
    sync_run_id: UUID | None = None,
) -> PitChangeIndexGeneration:
    """Insert or reuse one immutable generation without mutating prior evidence."""
    existing = session.scalar(
        select(PitChangeIndexGeneration).where(
            PitChangeIndexGeneration.indicator_id == indicator_id,
            PitChangeIndexGeneration.provider == materialization.change_index.provider,
            PitChangeIndexGeneration.provider_series_id == provider_series_id,
            PitChangeIndexGeneration.generation_hash == discovery.generation_hash,
        )
    )
    if existing is not None:
        return existing
    prior = session.scalar(
        select(PitChangeIndexGeneration)
        .where(
            PitChangeIndexGeneration.indicator_id == indicator_id,
            PitChangeIndexGeneration.provider == materialization.change_index.provider,
            PitChangeIndexGeneration.provider_series_id == provider_series_id,
            PitChangeIndexGeneration.status == ChangeIndexStatus.COMPLETE.value,
        )
        .order_by(PitChangeIndexGeneration.id.desc())
        .limit(1)
    )
    record = PitChangeIndexGeneration(
        indicator_id=indicator_id,
        provider=materialization.change_index.provider,
        provider_series_id=provider_series_id,
        status=ChangeIndexStatus.COMPLETE.value,
        index_coverage_start=discovery.coverage_start,
        index_coverage_end=discovery.coverage_end,
        index_complete_through=discovery.complete_through,
        provider_count=discovery.provider_count,
        retrieved_count=len(discovery.change_dates),
        page_count=len(discovery.pages),
        change_dates_json=[item.isoformat() for item in discovery.change_dates],
        pagination_evidence_json=[item.as_payload() for item in discovery.pages],
        generation_hash=discovery.generation_hash,
        source_registry_hash=source_registry_hash,
        pit_materialization_spec_version=materialization.pit_materialization_spec_version,
        pit_materialization_spec_hash=materialization.specification_hash,
        sync_run_id=sync_run_id,
        supersedes_generation_id=prior.id if prior is not None else None,
        discovered_at=discovered_at,
    )
    session.add(record)
    session.flush()
    return record


def authoritative_generation(
    session: Session,
    *,
    indicator_id: str,
    provider_series_id: str,
    evaluation_as_of: date,
    source_registry_hash: str,
    materialization: PitMaterializationCatalog,
) -> PitChangeIndexGeneration:
    """Return the newest complete generation covering one evaluation EOD."""
    generation = session.scalar(
        select(PitChangeIndexGeneration)
        .where(
            PitChangeIndexGeneration.indicator_id == indicator_id,
            PitChangeIndexGeneration.provider == materialization.change_index.provider,
            PitChangeIndexGeneration.provider_series_id == provider_series_id,
            PitChangeIndexGeneration.status == ChangeIndexStatus.COMPLETE.value,
            PitChangeIndexGeneration.index_coverage_start <= evaluation_as_of,
            PitChangeIndexGeneration.index_complete_through >= evaluation_as_of,
            PitChangeIndexGeneration.source_registry_hash == source_registry_hash,
            PitChangeIndexGeneration.pit_materialization_spec_hash
            == materialization.specification_hash,
        )
        .order_by(PitChangeIndexGeneration.id.desc())
        .limit(1)
    )
    if generation is not None:
        return generation
    latest = session.scalar(
        select(PitChangeIndexGeneration)
        .where(
            PitChangeIndexGeneration.indicator_id == indicator_id,
            PitChangeIndexGeneration.provider_series_id == provider_series_id,
        )
        .order_by(PitChangeIndexGeneration.id.desc())
        .limit(1)
    )
    if latest is not None and latest.index_complete_through is not None:
        if evaluation_as_of > latest.index_complete_through:
            raise ChangeIndexStale("PIT_CHANGE_INDEX_STALE:REFRESH_REQUIRED")
        if latest.status != ChangeIndexStatus.COMPLETE.value:
            raise ChangeIndexIncomplete("PIT_CHANGE_INDEX_INCOMPLETE")
    raise ChangeIndexUnavailable("PIT_CHANGE_INDEX_UNAVAILABLE")


def source_information_set_date(
    generation: PitChangeIndexGeneration, evaluation_as_of: date
) -> date:
    """Select the newest official change on or before the evaluation EOD."""
    changes = tuple(date.fromisoformat(item) for item in generation.change_dates_json)
    if not changes:
        raise ChangeIndexNoVintage("PIT_NO_VINTAGE")
    position = bisect_right(changes, evaluation_as_of)
    if position == 0:
        raise ChangeIndexOutsideCoverage("PIT_PROVEN_OUTSIDE_COVERAGE")
    return changes[position - 1]


def affected_intervals(
    old_dates: tuple[date, ...], new_dates: tuple[date, ...]
) -> list[tuple[date, date | None]]:
    """Return precise changed resolution intervals for cache invalidation."""
    changed = sorted(set(old_dates) ^ set(new_dates))
    union = sorted(set(old_dates) | set(new_dates))
    intervals: list[tuple[date, date | None]] = []
    for changed_date in changed:
        next_date = next((item for item in union if item > changed_date), None)
        intervals.append((changed_date, next_date))
    return intervals
