"""Reviewed authority for observation periods an official source will never publish."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.source_registry.config import load_source_registry
from app.source_registry.schemas import SourceRegistryCatalog

DEFAULT_PERMANENT_GAP_PATH = Path("config/data_sources/permanent_gaps.yaml")


class PermanentGap(BaseModel):
    """One officially confirmed, never-to-be-published observation period."""

    model_config = ConfigDict(extra="forbid")

    gap_id: str = Field(min_length=1)
    observation_period: date
    indicator_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)
    official_source_url: str
    known_since: date
    verified_at: date

    @field_validator("official_source_url")
    @classmethod
    def validate_official_source_url(cls, value: str) -> str:
        """Require reviewable HTTPS evidence for every declared gap."""
        if not value.startswith("https://"):
            raise ValueError("permanent gap evidence must be an https URL")
        return value


class PermanentGapCatalog(BaseModel):
    """Hashed, registry-bound set of declared permanent gaps."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    permanent_gap_version: str
    required_source_registry_version: str
    required_source_registry_hash: str
    gaps: list[PermanentGap]
    source_path: Path
    permanent_gap_hash: str

    def periods_for(self, indicator_id: str, *, as_of: date | None = None) -> frozenset[date]:
        """Return declared periods for one indicator that were known at ``as_of``.

        ``as_of=None`` is current mode, where every declared gap applies. A
        point-in-time evaluation only sees gaps the source had already confirmed,
        so the rule never leaks later knowledge into an earlier information set.
        """
        return frozenset(
            gap.observation_period
            for gap in self.gaps
            if indicator_id in gap.indicator_ids and (as_of is None or gap.known_since <= as_of)
        )


def load_permanent_gap_catalog(
    path: Path = DEFAULT_PERMANENT_GAP_PATH,
    *,
    source_registry: SourceRegistryCatalog | None = None,
) -> PermanentGapCatalog:
    """Load, bind, validate, and hash the permanent-gap authority."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("gaps"), list):
        raise ValueError("permanent gap authority requires a gaps list")
    gaps = [PermanentGap.model_validate(item) for item in raw["gaps"]]
    registry = source_registry or load_source_registry()
    required_version = str(raw["required_source_registry_version"])
    required_hash = str(raw["required_source_registry_hash"])
    if required_version != registry.registry_policy_version:
        raise ValueError("permanent gap source registry version mismatch")
    if required_hash != registry.registry_hash:
        raise ValueError("permanent gap source registry hash mismatch")
    gap_ids = [gap.gap_id for gap in gaps]
    if len(gap_ids) != len(set(gap_ids)):
        raise ValueError("permanent gap IDs must be unique")
    by_indicator = registry.by_indicator()
    declared: set[tuple[str, date]] = set()
    for gap in gaps:
        if gap.known_since < gap.observation_period:
            raise ValueError(f"{gap.gap_id} cannot be known before its observation period")
        for indicator_id in gap.indicator_ids:
            policy = by_indicator.get(indicator_id)
            if policy is None:
                raise ValueError(f"{gap.gap_id} references unknown indicator {indicator_id}")
            if policy.canonical.frequency != "weekly" and gap.observation_period.day != 1:
                raise ValueError(f"{gap.gap_id} must use the native period start date")
            key = (indicator_id, gap.observation_period)
            if key in declared:
                raise ValueError(f"{gap.gap_id} declares {indicator_id} twice for one period")
            declared.add(key)
    hash_payload = {
        "schema_version": str(raw["schema_version"]),
        "permanent_gap_version": str(raw["permanent_gap_version"]),
        "required_source_registry_version": required_version,
        "required_source_registry_hash": required_hash,
        "gaps": [gap.model_dump(mode="json") for gap in sorted(gaps, key=lambda item: item.gap_id)],
    }
    authority_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return PermanentGapCatalog(
        schema_version=str(raw["schema_version"]),
        permanent_gap_version=str(raw["permanent_gap_version"]),
        required_source_registry_version=required_version,
        required_source_registry_hash=required_hash,
        gaps=gaps,
        source_path=path,
        permanent_gap_hash=authority_hash,
    )
