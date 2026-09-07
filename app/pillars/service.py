"""Compute Stage 3C pillars exclusively from Stage 3B normalized signals."""

import hashlib
import json
from collections import defaultdict
from collections.abc import Awaitable, Sequence
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Protocol

from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from app.pillars.cache import PillarCacheKey, PillarResultCache
from app.pillars.calculations import (
    EXTREME_CONTRIBUTION_THRESHOLD,
    concentration_metadata,
    coverage_status,
    decimal_tanh,
    supporting_confirmation,
)
from app.pillars.config import load_pillar_catalog
from app.pillars.schemas import (
    ConfidenceComponents,
    ContributionMappingParameters,
    CoveragePolicy,
    PillarCatalog,
    PillarDefinition,
    PillarQualityFlag,
    PillarResult,
    PillarStatus,
    SupportingConfirmationStatus,
    SupportingSignalDefinition,
)
from app.signals.config import load_signal_catalog
from app.signals.schemas import (
    NormalizedSignalResult,
    SignalCatalog,
    SignalQualityFlag,
    SignalStatus,
)
from app.signals.service import SignalEngine
from app.source_registry.config import load_source_registry
from app.source_registry.materialization import load_pit_materialization_catalog
from app.source_registry.revision_provenance import (
    RevisionProvenanceCatalog,
    build_layer_effect,
    load_revision_provenance_catalog,
)
from app.source_registry.revision_schemas import RevisionLayerEffect
from app.source_registry.schemas import SourceRegistryCatalog

_PROCESS_CACHE = PillarResultCache(max_entries=128)


class SignalService(Protocol):
    """Minimal Stage 3B boundary accepted by the Stage 3C service."""

    def compute_signal_set(
        self,
        signal_ids: Sequence[str],
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> Awaitable[list[NormalizedSignalResult]]:
        """Return configured normalized signals only."""
        ...


class PillarEngineError(RuntimeError):
    """Base fail-closed Stage 3C service error."""


class PillarNotFound(PillarEngineError):
    """A stable pillar ID is absent from the specification."""


class PillarContractMismatch(PillarEngineError):
    """Pillar and upstream authorities do not match."""


def _hash_payload(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _latest_by_signal(
    rows: Sequence[NormalizedSignalResult], evaluation_date: date
) -> dict[str, NormalizedSignalResult]:
    """Select the newest result known by the shared EOD, including newest errors."""
    latest: dict[str, NormalizedSignalResult] = {}
    for row in rows:
        if row.feature_observation_date > evaluation_date:
            continue
        previous = latest.get(row.signal_id)
        if previous is None or row.feature_observation_date > previous.feature_observation_date:
            latest[row.signal_id] = row
    return latest


def _pillar_revision_effect(
    *,
    primary_rows: Sequence[NormalizedSignalResult | None],
    all_rows: Sequence[NormalizedSignalResult],
    current_value: Decimal | None,
    current_status: PillarStatus,
    parameters: ContributionMappingParameters,
    coverage_policy: CoveragePolicy,
    authority: RevisionProvenanceCatalog,
) -> RevisionLayerEffect | None:
    """Compare a pillar with canonical predecessor signal values, never prior calls."""
    effects = [item.revision_effect for item in all_rows if item.revision_effect is not None]
    if not effects:
        return None
    evidence_hashes = tuple(
        sorted(
            {
                evidence_hash
                for effect in effects
                for evidence_hash in effect.source_revision_evidence_hashes
            }
        )
    )
    source_effects = tuple(
        sorted(
            {source_effect for effect in effects for source_effect in effect.source_effect_types},
            key=str,
        )
    )
    counterfactual_values: list[Decimal] = []
    for row in primary_rows:
        if row is None:
            continue
        effect = row.revision_effect
        value = effect.counterfactual_value if effect is not None else row.directed_value
        status_value = (
            effect.counterfactual_status if effect is not None else row.signal_status.value
        )
        try:
            status = SignalStatus(status_value)
        except ValueError as error:
            raise PillarEngineError("INVALID_SIGNAL_REVISION_STATUS") from error
        if status is SignalStatus.OK and value is not None:
            counterfactual_values.append(value)
    _, counterfactual_status = coverage_status(
        len(counterfactual_values), len(primary_rows), coverage_policy
    )
    if current_status in {
        PillarStatus.PIT_UNSAFE,
        PillarStatus.SIGNAL_ERROR,
        PillarStatus.SPEC_MISMATCH,
    }:
        counterfactual_status = current_status
    counterfactual_value: Decimal | None = None
    if counterfactual_status in {PillarStatus.OK, PillarStatus.PARTIAL}:
        contributions = [
            decimal_tanh(item, amplitude=parameters.amplitude, scale=parameters.scale)
            for item in counterfactual_values
        ]
        with localcontext() as context:
            context.prec = 50
            counterfactual_value = sum(contributions, start=Decimal(0)) / Decimal(
                len(contributions)
            )
    return build_layer_effect(
        authority=authority,
        source_evidence_hashes=evidence_hashes,
        source_effect_types=source_effects,
        current_value=current_value,
        counterfactual_value=counterfactual_value,
        current_status=current_status.value,
        counterfactual_status=counterfactual_status.value,
    )


class PillarEngine:
    """Aggregate latest same-EOD Stage 3B signals into bounded semantic pillars."""

    def __init__(
        self,
        session: Session,
        *,
        source_registry: SourceRegistryCatalog | None = None,
        signal_catalog: SignalCatalog | None = None,
        pillar_catalog: PillarCatalog | None = None,
        signal_engine: SignalService | None = None,
        cache: PillarResultCache | None = None,
    ) -> None:
        self.session = session
        self.source_registry = source_registry or load_source_registry()
        self.signal_catalog = signal_catalog or load_signal_catalog(
            source_registry=self.source_registry
        )
        self.pillar_catalog = pillar_catalog or load_pillar_catalog(
            source_registry=self.source_registry,
            signal_catalog=self.signal_catalog,
        )
        if (
            self.pillar_catalog.required_source_registry_version
            != self.source_registry.registry_policy_version
            or self.pillar_catalog.required_source_registry_hash
            != self.source_registry.registry_hash
            or self.pillar_catalog.required_normalization_spec_version
            != self.signal_catalog.normalization_spec_version
            or self.pillar_catalog.required_normalization_spec_hash
            != self.signal_catalog.specification_hash
        ):
            raise PillarContractMismatch("PILLAR_CONTRACT_MISMATCH")
        self.signal_engine = signal_engine or SignalEngine(
            session,
            source_registry=self.source_registry,
            signal_catalog=self.signal_catalog,
        )
        self.cache = cache if cache is not None else PillarResultCache()
        materialization = load_pit_materialization_catalog(source_registry=self.source_registry)
        self.revision_provenance = load_revision_provenance_catalog(
            source_registry=self.source_registry,
            materialization=materialization,
        )

    def _cache_key(
        self,
        definition: PillarDefinition,
        latest: dict[str, NormalizedSignalResult],
        evaluation_as_of: date | None,
        failures: dict[str, str],
    ) -> PillarCacheKey:
        primary_hashes = tuple(
            latest[signal_id].calculation_hash
            if signal_id in latest
            else f"ERROR:{failures[signal_id]}"
            if signal_id in failures
            else "MISSING"
            for signal_id in definition.primary_signal_ids
        )
        supporting_hashes = tuple(
            latest[item.signal_id].calculation_hash
            if item.signal_id in latest
            else f"ERROR:{failures[item.signal_id]}"
            if item.signal_id in failures
            else "MISSING"
            for item in definition.supporting_signals
        )
        return PillarCacheKey(
            pillar_id=definition.pillar_id,
            pillar_spec_version=self.pillar_catalog.pillar_spec_version,
            pillar_spec_hash=self.pillar_catalog.specification_hash,
            evaluation_context=(evaluation_as_of.isoformat() if evaluation_as_of else "CURRENT"),
            normalization_spec_hash=self.signal_catalog.specification_hash,
            registry_hash=self.source_registry.registry_hash,
            constituent_calculation_hashes=primary_hashes,
            supporting_input_hashes=supporting_hashes,
        )

    def _input_contract_status(
        self,
        rows: Sequence[NormalizedSignalResult],
        evaluation_as_of: date | None,
    ) -> PillarStatus | None:
        for row in rows:
            if (
                row.registry_hash != self.source_registry.registry_hash
                or row.feature_spec_version != self.pillar_catalog.required_feature_spec_version
                or row.normalization_spec_version
                != self.pillar_catalog.required_normalization_spec_version
                or row.normalization_spec_hash
                != self.pillar_catalog.required_normalization_spec_hash
            ):
                return PillarStatus.SPEC_MISMATCH
        if evaluation_as_of is not None:
            for row in rows:
                if (
                    row.as_of != evaluation_as_of
                    or not row.is_point_in_time_safe
                    or row.signal_status is SignalStatus.PIT_UNSAFE
                ):
                    return PillarStatus.PIT_UNSAFE
        elif any(row.as_of is not None for row in rows):
            return PillarStatus.PIT_UNSAFE
        return None

    def _build_result(
        self,
        definition: PillarDefinition,
        latest: dict[str, NormalizedSignalResult],
        evaluation_date: date,
        evaluation_as_of: date | None,
        *,
        failures: dict[str, str] | None = None,
        forced_status: PillarStatus | None = None,
    ) -> PillarResult:
        signal_failures = failures or {}
        signal_definitions = self.signal_catalog.by_signal()
        primary_rows = [latest.get(signal_id) for signal_id in definition.primary_signal_ids]
        input_rows = [row for row in primary_rows if row is not None]
        input_rows.extend(
            latest[item.signal_id]
            for item in definition.supporting_signals
            if item.signal_id in latest
        )
        contract_status = self._input_contract_status(input_rows, evaluation_as_of)
        if contract_status is not None:
            forced_status = contract_status

        valid_rows = [
            row
            for row in primary_rows
            if row is not None
            and row.signal_status is SignalStatus.OK
            and row.directed_value is not None
        ]
        coverage, coverage_state = coverage_status(
            len(valid_rows), len(primary_rows), self.pillar_catalog.coverage_policy
        )
        parameters = self.pillar_catalog.contribution_mapping_parameters
        contributions: dict[str, Decimal | None] = {}
        pre_map: dict[str, Decimal | None] = {}
        for signal_id, row in zip(definition.primary_signal_ids, primary_rows, strict=True):
            directed = row.directed_value if row is not None else None
            pre_map[signal_id] = directed
            contributions[signal_id] = (
                decimal_tanh(
                    directed,
                    amplitude=parameters.amplitude,
                    scale=parameters.scale,
                )
                if row is not None and row.signal_status is SignalStatus.OK and directed is not None
                else None
            )

        status = forced_status or coverage_state
        value: Decimal | None = None
        if status in {PillarStatus.OK, PillarStatus.PARTIAL}:
            valid_contributions = [item for item in contributions.values() if item is not None]
            with localcontext() as context:
                context.prec = 50
                value = sum(valid_contributions, start=Decimal(0)) / Decimal(
                    len(valid_contributions)
                )

        grouped_supporting: dict[str, list[SupportingSignalDefinition]] = defaultdict(list)
        for item in definition.supporting_signals:
            grouped_supporting[item.redundancy_group].append(item)
        diagnostics = []
        for index, primary in enumerate(primary_rows):
            if primary is not None:
                group = primary.redundancy_group
                primary_id = primary.signal_id
            else:
                primary_id = definition.primary_signal_ids[index]
                group = signal_definitions[primary_id].redundancy_group
            diagnostics.append(
                supporting_confirmation(
                    primary,
                    grouped_supporting[group],
                    latest,
                    primary_signal_id=primary_id,
                )
            )

        flags: set[PillarQualityFlag] = set()
        all_quality_flags = {flag for row in input_rows for flag in row.quality_flags}
        if SignalQualityFlag.DEGRADED_INPUT in all_quality_flags:
            flags.add(PillarQualityFlag.DEGRADED_INPUT)
        if SignalQualityFlag.FALLBACK_INPUT in all_quality_flags:
            flags.add(PillarQualityFlag.FALLBACK_INPUT)
        if any(
            item is not None and abs(item) >= EXTREME_CONTRIBUTION_THRESHOLD
            for item in pre_map.values()
        ):
            flags.add(PillarQualityFlag.EXTREME_CONTRIBUTION)
        if any(
            item.status is SupportingConfirmationStatus.DIVERGES_FROM_PRIMARY
            for item in diagnostics
        ):
            flags.add(PillarQualityFlag.DIVERGENT_SUPPORTING_SIGNALS)
        if coverage < Decimal(1):
            flags.add(PillarQualityFlag.LOW_COVERAGE)
        if definition.concentration_warning:
            flags.add(PillarQualityFlag.CONCENTRATION_WARNING)

        revision_effect = _pillar_revision_effect(
            primary_rows=primary_rows,
            all_rows=input_rows,
            current_value=value,
            current_status=status,
            parameters=parameters,
            coverage_policy=self.pillar_catalog.coverage_policy,
            authority=self.revision_provenance,
        )
        if revision_effect is not None and revision_effect.result_changed:
            flags.add(PillarQualityFlag.REVISION_CHANGED_RESULT)

        expected_count = len(primary_rows)
        healthy_count = sum(
            row is not None
            and row.signal_status is SignalStatus.OK
            and row.directed_value is not None
            and SignalQualityFlag.DEGRADED_INPUT not in row.quality_flags
            for row in primary_rows
        )
        fresh_count = sum(
            row is not None
            and row.signal_status is SignalStatus.OK
            and row.directed_value is not None
            and SignalQualityFlag.STALE_INPUT not in row.quality_flags
            for row in primary_rows
        )
        resolved = [
            item
            for item in diagnostics
            if item.status is not SupportingConfirmationStatus.INSUFFICIENT
        ]
        confirms = sum(
            item.status is SupportingConfirmationStatus.CONFIRMS_PRIMARY for item in resolved
        )
        confidence_components = ConfidenceComponents(
            coverage=coverage,
            source_health_coverage=Decimal(healthy_count) / Decimal(expected_count),
            freshness_coverage=Decimal(fresh_count) / Decimal(expected_count),
            agreement_ratio=(Decimal(confirms) / Decimal(len(resolved)) if resolved else None),
            revision_state=(
                "CHANGED" if PillarQualityFlag.REVISION_CHANGED_RESULT in flags else "UNCHANGED"
            ),
        )
        axes = [
            signal_definitions[item].economic_axis.value for item in definition.primary_signal_ids
        ]
        groups = [
            signal_definitions[item].redundancy_group for item in definition.primary_signal_ids
        ]
        concentration = concentration_metadata(
            primary_rows,
            expected_primary_count=expected_count,
            expected_axes=axes,
            expected_groups=groups,
        )

        observation_dates = {
            signal_id: row.feature_observation_date if row is not None else None
            for signal_id, row in zip(definition.primary_signal_ids, primary_rows, strict=True)
        }
        statuses = {
            signal_id: row.signal_status.value
            if row is not None
            else "SIGNAL_ERROR"
            if signal_id in signal_failures
            else "MISSING"
            for signal_id, row in zip(definition.primary_signal_ids, primary_rows, strict=True)
        }
        constituent_hashes = {
            signal_id: row.calculation_hash if row is not None else None
            for signal_id, row in zip(definition.primary_signal_ids, primary_rows, strict=True)
        }
        payload: dict[str, object] = {
            "pillar_spec_hash": self.pillar_catalog.specification_hash,
            "pillar_id": definition.pillar_id,
            "evaluation_as_of": evaluation_date.isoformat(),
            "information_set": evaluation_as_of.isoformat() if evaluation_as_of else "CURRENT",
            "constituent_hashes": constituent_hashes,
            "supporting_hashes": {
                item.signal_id: latest[item.signal_id].calculation_hash
                if item.signal_id in latest
                else None
                for item in definition.supporting_signals
            },
            "signal_failures": {key: signal_failures[key] for key in sorted(signal_failures)},
            "value": str(value) if value is not None else None,
            "coverage": str(coverage),
            "status": status.value,
            "flags": sorted(item.value for item in flags),
        }
        if evaluation_as_of is not None:
            payload.update(
                revision_provenance_version=(self.revision_provenance.revision_provenance_version),
                revision_provenance_hash=self.revision_provenance.specification_hash,
                revision_effect_hash=(
                    revision_effect.revision_effect_hash if revision_effect is not None else None
                ),
            )
        result = PillarResult(
            pillar_id=definition.pillar_id,
            display_name=definition.display_name,
            description=definition.description,
            pillar_role=definition.pillar_role,
            core_composite_eligible=definition.core_composite_eligible,
            pillar_spec_version=self.pillar_catalog.pillar_spec_version,
            pillar_spec_hash=self.pillar_catalog.specification_hash,
            evaluation_as_of=evaluation_date,
            value=value,
            unit="bounded_symmetric_score",
            semantic_direction=definition.semantic_direction,
            confidence=coverage,
            confidence_components=confidence_components,
            aggregation_method=self.pillar_catalog.aggregation_method,
            contribution_mapping=self.pillar_catalog.contribution_mapping,
            contribution_mapping_parameters=parameters,
            expected_constituent_count=expected_count,
            valid_constituent_count=len(valid_rows),
            expected_weight=Decimal(expected_count),
            available_weight=Decimal(len(valid_rows)),
            coverage=coverage,
            constituent_signal_ids=definition.primary_signal_ids,
            constituent_observation_dates=observation_dates,
            constituent_normalized_values={
                signal_id: row.normalized_value if row is not None else None
                for signal_id, row in zip(definition.primary_signal_ids, primary_rows, strict=True)
            },
            constituent_directed_values={
                signal_id: row.directed_value if row is not None else None
                for signal_id, row in zip(definition.primary_signal_ids, primary_rows, strict=True)
            },
            constituent_pre_map_values=pre_map,
            constituent_contributions=contributions,
            constituent_weights={
                signal_id: Decimal(1) for signal_id in definition.primary_signal_ids
            },
            constituent_statuses=statuses,
            constituent_quality_flags={
                signal_id: [flag.value for flag in row.quality_flags] if row is not None else []
                for signal_id, row in zip(definition.primary_signal_ids, primary_rows, strict=True)
            },
            constituent_calculation_hashes=constituent_hashes,
            supporting_confirmation=diagnostics,
            concentration=concentration,
            source_registry_hash=self.source_registry.registry_hash,
            feature_spec_version=self.pillar_catalog.required_feature_spec_version,
            normalization_spec_version=self.signal_catalog.normalization_spec_version,
            normalization_spec_hash=self.signal_catalog.specification_hash,
            is_point_in_time_safe=(
                evaluation_as_of is not None
                and status
                not in {
                    PillarStatus.PIT_UNSAFE,
                    PillarStatus.SPEC_MISMATCH,
                    PillarStatus.SIGNAL_ERROR,
                }
            ),
            pillar_status=status,
            quality_flags=sorted(flags, key=str),
            calculation_hash=_hash_payload(payload),
            revision_provenance_version=(
                self.revision_provenance.revision_provenance_version
                if evaluation_as_of is not None
                else None
            ),
            revision_provenance_hash=(
                self.revision_provenance.specification_hash
                if evaluation_as_of is not None
                else None
            ),
            revision_evidence_hashes=(
                revision_effect.source_revision_evidence_hashes
                if revision_effect is not None
                else ()
            ),
            revision_effect=revision_effect,
        )
        return result

    async def compute_pillar_set(
        self,
        pillar_ids: Sequence[str],
        evaluation_as_of: date | None = None,
    ) -> list[PillarResult]:
        """Compute selected pillars from a single same-EOD Stage 3B information set."""
        by_pillar = self.pillar_catalog.by_pillar()
        requested: list[PillarDefinition] = []
        seen: set[str] = set()
        for pillar_id in pillar_ids:
            if pillar_id in seen:
                continue
            seen.add(pillar_id)
            try:
                requested.append(by_pillar[pillar_id])
            except KeyError as error:
                raise PillarNotFound(f"UNKNOWN_PILLAR:{pillar_id}") from error
        evaluation_date = evaluation_as_of or date.today()
        start = evaluation_date - relativedelta(
            months=self.pillar_catalog.latest_known_search_months
        )
        signal_ids = list(
            dict.fromkeys(
                signal_id
                for definition in requested
                for signal_id in [
                    *definition.primary_signal_ids,
                    *(item.signal_id for item in definition.supporting_signals),
                ]
            )
        )
        rows: list[NormalizedSignalResult] = []
        failures: dict[str, str] = {}
        for signal_id in signal_ids:
            try:
                rows.extend(
                    await self.signal_engine.compute_signal_set(
                        [signal_id],
                        start,
                        evaluation_date,
                        evaluation_as_of,
                    )
                )
            except Exception as error:
                failures[signal_id] = type(error).__name__
        latest = _latest_by_signal(rows, evaluation_date)
        results: list[PillarResult] = []
        for definition in requested:
            relevant_ids = {
                *definition.primary_signal_ids,
                *(item.signal_id for item in definition.supporting_signals),
            }
            relevant = {key: value for key, value in latest.items() if key in relevant_ids}
            relevant_failures = {
                key: value for key, value in failures.items() if key in relevant_ids
            }
            key = self._cache_key(definition, relevant, evaluation_as_of, relevant_failures)
            cached = self.cache.get(key)
            if cached is not None:
                results.append(cached)
                continue
            result = self._build_result(
                definition,
                relevant,
                evaluation_date,
                evaluation_as_of,
                failures=relevant_failures,
            )
            self.cache.put(key, result)
            results.append(result)
        return results

    async def compute_pillar(
        self, pillar_id: str, evaluation_as_of: date | None = None
    ) -> PillarResult:
        """Compute one configured pillar."""
        return (await self.compute_pillar_set([pillar_id], evaluation_as_of))[0]

    async def compute_all_pillars(self, evaluation_as_of: date | None = None) -> list[PillarResult]:
        """Compute all nine pillars without any Stage 3D composite."""
        return await self.compute_pillar_set(
            [item.pillar_id for item in self.pillar_catalog.pillars], evaluation_as_of
        )


async def compute_pillar(
    session: Session, pillar_id: str, evaluation_as_of: date | None = None
) -> PillarResult:
    """Public domain API for one configured Stage 3C pillar."""
    return await PillarEngine(session, cache=_PROCESS_CACHE).compute_pillar(
        pillar_id, evaluation_as_of
    )


async def compute_pillar_set(
    session: Session,
    pillar_ids: Sequence[str],
    evaluation_as_of: date | None = None,
) -> list[PillarResult]:
    """Public domain API for selected configured Stage 3C pillars."""
    return await PillarEngine(session, cache=_PROCESS_CACHE).compute_pillar_set(
        pillar_ids, evaluation_as_of
    )


async def compute_all_pillars(
    session: Session, evaluation_as_of: date | None = None
) -> list[PillarResult]:
    """Public domain API for all configured Stage 3C pillars."""
    return await PillarEngine(session, cache=_PROCESS_CACHE).compute_all_pillars(evaluation_as_of)
