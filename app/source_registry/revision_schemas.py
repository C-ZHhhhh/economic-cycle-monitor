"""Typed canonical revision evidence shared by source and Stage 3 layers."""

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RevisionEffectType(StrEnum):
    """Canonical source and downstream revision-effect vocabulary."""

    NEW_INFORMATION = "NEW_INFORMATION"
    SOURCE_REVISION = "SOURCE_REVISION"
    OBSERVATION_REMOVAL = "OBSERVATION_REMOVAL"
    OBSERVATION_STATUS_CHANGE = "OBSERVATION_STATUS_CHANGE"
    DISTRIBUTION_CHANGE = "DISTRIBUTION_CHANGE"
    RESULT_CHANGE = "RESULT_CHANGE"


class RevisionEvidenceStatus(StrEnum):
    """Whether a persisted predecessor exists and proves a semantic revision."""

    NO_COMPARABLE_PREDECESSOR = "NO_COMPARABLE_PREDECESSOR"
    NO_REVISION_DETECTED = "NO_REVISION_DETECTED"
    REVISION_DETECTED = "REVISION_DETECTED"


class RevisionPredecessorKind(StrEnum):
    """The immutable relationship used as the comparison authority."""

    NO_PREDECESSOR = "NO_PREDECESSOR"
    SAME_DATE_CORRECTION = "SAME_DATE_CORRECTION"
    PREVIOUS_CHANGE_DATE = "PREVIOUS_CHANGE_DATE"


class CanonicalObservationStatus(StrEnum):
    """Canonical availability states represented by an information set."""

    ABSENT = "ABSENT"
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"


class ObservationRevisionDelta(BaseModel):
    """One sorted canonical observation-state difference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_date: date
    prior_status: CanonicalObservationStatus
    current_status: CanonicalObservationStatus
    prior_value: Decimal | None = None
    current_value: Decimal | None = None
    effect_type: RevisionEffectType


class ObservationSetRevisionEnvelope(BaseModel):
    """Deterministic semantic diff between a PIT set and its persisted predecessor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision_provenance_version: str
    revision_provenance_hash: str
    indicator_id: str
    provider: str
    provider_series_id: str
    index_generation_hash: str
    current_information_set_hash: str
    predecessor_information_set_hash: str | None
    current_source_information_set_date: date
    predecessor_source_information_set_date: date | None
    predecessor_kind: RevisionPredecessorKind
    current_observation_count: int
    predecessor_observation_count: int
    added_observation_dates: tuple[date, ...]
    removed_observation_dates: tuple[date, ...]
    revised_value_dates: tuple[date, ...]
    status_changed_dates: tuple[date, ...]
    observation_deltas: tuple[ObservationRevisionDelta, ...]
    canonical_semantic_diff_hash: str
    effect_types: tuple[RevisionEffectType, ...]
    evidence_status: RevisionEvidenceStatus
    revision_evidence_hash: str

    @property
    def has_canonical_revision(self) -> bool:
        """Return whether the envelope proves a revision rather than normal new information."""
        revision_types = {
            RevisionEffectType.SOURCE_REVISION,
            RevisionEffectType.OBSERVATION_REMOVAL,
            RevisionEffectType.OBSERVATION_STATUS_CHANGE,
        }
        return bool(revision_types.intersection(self.effect_types))


class RevisionLayerEffect(BaseModel):
    """Canonical result impact propagated through one Stage 3 layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision_provenance_version: str
    revision_provenance_hash: str
    source_revision_evidence_hashes: tuple[str, ...]
    source_effect_types: tuple[RevisionEffectType, ...]
    effect_types: tuple[RevisionEffectType, ...]
    current_value: Decimal | None
    counterfactual_value: Decimal | None
    current_status: str
    counterfactual_status: str
    result_changed: bool
    distribution_changed: bool = False
    current_result_hash: str
    counterfactual_result_hash: str
    revision_effect_hash: str
