"""Build Stage 3D macro state exclusively from Stage 3C PillarResult objects."""

import hashlib
import json
from collections.abc import Awaitable, Sequence
from datetime import date
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from app.composites.cache import MacroStateCacheKey, MacroStateResultCache
from app.composites.calculations import (
    conditional_weighted_mean,
    expected_weighted_confidence,
    population_std,
    signed_breadth,
)
from app.composites.config import load_composite_catalog
from app.composites.schemas import (
    CompositeCatalog,
    EvaluationMode,
    GrowthComponentResult,
    GrowthStatus,
    HistoricalCoverageState,
    MacroStateQualityFlag,
    MacroStateResult,
    MacroStateStatus,
    PillarComponentSnapshot,
)
from app.pillars.config import load_pillar_catalog
from app.pillars.schemas import PillarCatalog, PillarResult, PillarStatus
from app.pillars.service import PillarEngine
from app.source_registry.config import load_source_registry
from app.source_registry.materialization import load_pit_materialization_catalog
from app.source_registry.revision_provenance import (
    RevisionProvenanceCatalog,
    build_layer_effect,
    load_revision_provenance_catalog,
)
from app.source_registry.revision_schemas import RevisionLayerEffect

_PROCESS_CACHE = MacroStateResultCache(max_entries=128)


class PillarService(Protocol):
    """Minimal Stage 3C boundary accepted by Stage 3D."""

    def compute_all_pillars(
        self, evaluation_as_of: date | None = None
    ) -> Awaitable[list[PillarResult]]:
        """Return the complete same-EOD Stage 3C pillar universe."""
        ...


class MacroStateEngineError(RuntimeError):
    """Base Stage 3D service error."""


class CompositeContractMismatch(MacroStateEngineError):
    """Composite and Stage 3C authorities do not match."""


def _hash_payload(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _macro_revision_effect(
    rows: Sequence[PillarResult],
    *,
    current_status: MacroStateStatus,
    growth: GrowthComponentResult,
    inflation: PillarComponentSnapshot,
    context: Sequence[PillarComponentSnapshot],
    authority: RevisionProvenanceCatalog,
) -> RevisionLayerEffect | None:
    """Propagate deterministic pillar effects into the complete MacroState result."""
    effects = [item.revision_effect for item in rows if item.revision_effect is not None]
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
    changed = any(item.result_changed for item in effects)
    current_payload = {
        "growth": growth.model_dump(mode="json"),
        "inflation": inflation.model_dump(mode="json"),
        "context": [item.model_dump(mode="json") for item in context],
        "status": current_status.value,
    }
    counterfactual_payload = {
        "upstream_counterfactual_hashes": sorted(
            item.counterfactual_result_hash for item in effects
        ),
        "status": current_status.value,
    }
    return build_layer_effect(
        authority=authority,
        source_evidence_hashes=evidence_hashes,
        source_effect_types=source_effects,
        current_value=growth.value,
        counterfactual_value=growth.value,
        current_status=current_status.value,
        counterfactual_status=current_status.value,
        result_changed_override=changed,
        current_result_payload=current_payload,
        counterfactual_result_payload=counterfactual_payload,
    )


class MacroStateEngine:
    """Create a three-layer continuous state from one Stage 3C pillar set."""

    def __init__(
        self,
        session: Session,
        *,
        pillar_catalog: PillarCatalog | None = None,
        composite_catalog: CompositeCatalog | None = None,
        pillar_engine: PillarService | None = None,
        cache: MacroStateResultCache | None = None,
    ) -> None:
        self.session = session
        self.pillar_catalog = pillar_catalog or load_pillar_catalog()
        self.composite_catalog = composite_catalog or load_composite_catalog(
            pillar_catalog=self.pillar_catalog
        )
        if (
            self.composite_catalog.required_pillar_spec_version
            != self.pillar_catalog.pillar_spec_version
            or self.composite_catalog.required_pillar_spec_hash
            != self.pillar_catalog.specification_hash
            or self.composite_catalog.required_source_registry_version
            != self.pillar_catalog.required_source_registry_version
            or self.composite_catalog.required_source_registry_hash
            != self.pillar_catalog.required_source_registry_hash
            or self.composite_catalog.required_feature_spec_version
            != self.pillar_catalog.required_feature_spec_version
            or self.composite_catalog.required_normalization_spec_version
            != self.pillar_catalog.required_normalization_spec_version
            or self.composite_catalog.required_normalization_spec_hash
            != self.pillar_catalog.required_normalization_spec_hash
        ):
            raise CompositeContractMismatch("COMPOSITE_CONTRACT_MISMATCH")
        self.pillar_engine = pillar_engine or PillarEngine(
            session, pillar_catalog=self.pillar_catalog
        )
        self.cache = cache if cache is not None else MacroStateResultCache()
        registry = load_source_registry()
        materialization = load_pit_materialization_catalog(source_registry=registry)
        self.revision_provenance = load_revision_provenance_catalog(
            source_registry=registry,
            materialization=materialization,
        )

    def _ordered_ids(self) -> list[str]:
        return [
            *(item.pillar_id for item in self.composite_catalog.growth.pillars),
            self.composite_catalog.inflation_pillar_id,
            *self.composite_catalog.context_pillar_ids,
        ]

    def _cache_key(
        self, by_pillar: dict[str, PillarResult], evaluation_as_of: date | None
    ) -> MacroStateCacheKey:
        return MacroStateCacheKey(
            state_id=self.composite_catalog.state_id,
            composite_spec_version=self.composite_catalog.composite_spec_version,
            composite_spec_hash=self.composite_catalog.specification_hash,
            evaluation_context=evaluation_as_of.isoformat() if evaluation_as_of else "CURRENT",
            pillar_spec_version=self.pillar_catalog.pillar_spec_version,
            pillar_spec_hash=self.pillar_catalog.specification_hash,
            ordered_pillar_calculation_hashes=tuple(
                by_pillar[pillar_id].calculation_hash
                if pillar_id in by_pillar
                else f"MISSING:{pillar_id}"
                for pillar_id in self._ordered_ids()
            ),
        )

    def _component_snapshot(
        self,
        pillar_id: str,
        row: PillarResult | None,
        *,
        mask_value: bool,
    ) -> PillarComponentSnapshot:
        definition = self.pillar_catalog.by_pillar()[pillar_id]
        if row is None:
            return PillarComponentSnapshot(
                pillar_id=pillar_id,
                value=None,
                confidence=Decimal(0),
                status=PillarStatus.SIGNAL_ERROR,
                semantic_direction=definition.semantic_direction,
                calculation_hash=f"MISSING:{pillar_id}",
            )
        return PillarComponentSnapshot(
            pillar_id=pillar_id,
            value=None if mask_value else row.value,
            confidence=row.confidence,
            status=row.pillar_status,
            semantic_direction=row.semantic_direction,
            calculation_hash=row.calculation_hash,
        )

    def _input_failure(
        self,
        rows: Sequence[PillarResult],
        by_pillar: dict[str, PillarResult],
        evaluation_date: date,
        evaluation_as_of: date | None,
    ) -> MacroStateStatus | None:
        expected = set(self._ordered_ids())
        ids = [item.pillar_id for item in rows]
        if len(ids) != len(set(ids)) or set(ids) != expected:
            return MacroStateStatus.PILLAR_ERROR
        for row in rows:
            if (
                row.pillar_spec_version != self.pillar_catalog.pillar_spec_version
                or row.pillar_spec_hash != self.pillar_catalog.specification_hash
                or row.source_registry_hash != self.composite_catalog.required_source_registry_hash
                or row.feature_spec_version != self.composite_catalog.required_feature_spec_version
                or row.normalization_spec_version
                != self.composite_catalog.required_normalization_spec_version
                or row.normalization_spec_hash
                != self.composite_catalog.required_normalization_spec_hash
                or row.pillar_status is PillarStatus.SPEC_MISMATCH
            ):
                return MacroStateStatus.SPEC_MISMATCH
        if any(row.evaluation_as_of != evaluation_date for row in rows):
            return MacroStateStatus.AS_OF_MISMATCH
        if evaluation_as_of is not None and any(
            not row.is_point_in_time_safe or row.pillar_status is PillarStatus.PIT_UNSAFE
            for row in rows
        ):
            return MacroStateStatus.PIT_UNSAFE
        if any(row.pillar_status is PillarStatus.SIGNAL_ERROR for row in rows):
            return MacroStateStatus.PILLAR_ERROR
        return None

    def _growth_result(
        self,
        by_pillar: dict[str, PillarResult],
        *,
        forced_status: MacroStateStatus | None = None,
    ) -> GrowthComponentResult:
        definition = self.composite_catalog.growth
        pillar_ids = [item.pillar_id for item in definition.pillars]
        base_weights = {item.pillar_id: item.base_weight for item in definition.pillars}
        usable = {PillarStatus.OK, PillarStatus.PARTIAL}
        values = {
            pillar_id: (
                by_pillar[pillar_id].value
                if pillar_id in by_pillar
                and by_pillar[pillar_id].pillar_status in usable
                and forced_status is None
                else None
            )
            for pillar_id in pillar_ids
        }
        usable_confidences = {
            pillar_id: (by_pillar[pillar_id].confidence if values[pillar_id] is not None else None)
            for pillar_id in pillar_ids
        }
        value, effective_weights, coverage = conditional_weighted_mean(
            values,
            base_weights,
            minimum_valid=definition.minimum_growth_pillars,
        )
        confidence = expected_weighted_confidence(usable_confidences, base_weights)
        positive, negative, neutral, breadth = signed_breadth(values)
        dispersion = population_std(values)
        if forced_status is not None:
            status = GrowthStatus(forced_status.value)
        elif value is None:
            status = GrowthStatus.INSUFFICIENT_GROWTH_COVERAGE
        elif len([item for item in values.values() if item is not None]) < len(pillar_ids) or any(
            by_pillar[pillar_id].pillar_status is PillarStatus.PARTIAL
            for pillar_id in pillar_ids
            if values[pillar_id] is not None
        ):
            status = GrowthStatus.PARTIAL
        else:
            status = GrowthStatus.OK
        return GrowthComponentResult(
            component_id=definition.component_id,
            value=value,
            unit=definition.unit,
            semantic_direction=definition.semantic_direction,
            status=status,
            confidence=confidence,
            coverage=coverage,
            expected_pillar_count=len(pillar_ids),
            valid_pillar_count=sum(item is not None for item in values.values()),
            aggregation_method=definition.aggregation_method,
            pillar_ids=pillar_ids,
            pillar_values=values,
            pillar_confidences={
                pillar_id: by_pillar[pillar_id].confidence if pillar_id in by_pillar else Decimal(0)
                for pillar_id in pillar_ids
            },
            pillar_statuses={
                pillar_id: by_pillar[pillar_id].pillar_status
                if pillar_id in by_pillar
                else PillarStatus.SIGNAL_ERROR
                for pillar_id in pillar_ids
            },
            pillar_base_weights=base_weights,
            pillar_effective_weights=effective_weights,
            pillar_calculation_hashes={
                pillar_id: by_pillar[pillar_id].calculation_hash
                if pillar_id in by_pillar
                else f"MISSING:{pillar_id}"
                for pillar_id in pillar_ids
            },
            positive_pillar_count=positive,
            negative_pillar_count=negative,
            neutral_pillar_count=neutral,
            breadth=breadth,
            dispersion=dispersion,
        )

    def _build_result(
        self,
        rows: Sequence[PillarResult],
        evaluation_as_of: date | None,
    ) -> MacroStateResult:
        evaluation_date = evaluation_as_of or date.today()
        evaluation_mode = (
            EvaluationMode.PIT_DATE_EOD if evaluation_as_of is not None else EvaluationMode.CURRENT
        )
        by_pillar = {item.pillar_id: item for item in rows}
        failure = self._input_failure(rows, by_pillar, evaluation_date, evaluation_as_of)
        fail_closed = failure in {
            MacroStateStatus.SPEC_MISMATCH,
            MacroStateStatus.AS_OF_MISMATCH,
            MacroStateStatus.PIT_UNSAFE,
            MacroStateStatus.PILLAR_ERROR,
        }
        growth = self._growth_result(by_pillar, forced_status=failure if fail_closed else None)
        inflation_id = self.composite_catalog.inflation_pillar_id
        inflation = self._component_snapshot(
            inflation_id,
            by_pillar.get(inflation_id),
            mask_value=fail_closed,
        )
        context = [
            self._component_snapshot(
                pillar_id,
                by_pillar.get(pillar_id),
                mask_value=fail_closed,
            )
            for pillar_id in self.composite_catalog.context_pillar_ids
        ]
        context_partial = any(
            pillar_id not in by_pillar
            or by_pillar[pillar_id].value is None
            or by_pillar[pillar_id].pillar_status is not PillarStatus.OK
            for pillar_id in self.composite_catalog.context_pillar_ids
        )
        inflation_usable = inflation.value is not None and inflation.status in {
            PillarStatus.OK,
            PillarStatus.PARTIAL,
        }
        if failure is not None:
            macro_status = failure
        elif growth.value is None:
            macro_status = MacroStateStatus.INSUFFICIENT_GROWTH_COVERAGE
        elif not inflation_usable:
            macro_status = MacroStateStatus.MISSING_INFLATION
        elif (
            growth.status is GrowthStatus.PARTIAL
            or inflation.status is PillarStatus.PARTIAL
            or context_partial
        ):
            macro_status = MacroStateStatus.PARTIAL
        else:
            macro_status = MacroStateStatus.OK

        all_full = len(rows) == 9 and all(
            item.value is not None and item.pillar_status is PillarStatus.OK for item in rows
        )
        if failure is not None or growth.value is None:
            historical_coverage = HistoricalCoverageState.INSUFFICIENT
        elif all_full:
            historical_coverage = HistoricalCoverageState.FULL
        else:
            historical_coverage = HistoricalCoverageState.PARTIAL

        flags: set[MacroStateQualityFlag] = set()
        if failure is None and growth.coverage < Decimal(1):
            flags.add(MacroStateQualityFlag.LOW_GROWTH_COVERAGE)
        if failure is None and growth.confidence < Decimal(1):
            flags.add(MacroStateQualityFlag.LOW_PILLAR_CONFIDENCE)
        if failure is None and context_partial:
            flags.add(MacroStateQualityFlag.CONTEXT_PARTIAL)
        revision_effect = _macro_revision_effect(
            rows,
            current_status=macro_status,
            growth=growth,
            inflation=inflation,
            context=context,
            authority=self.revision_provenance,
        )
        if revision_effect is not None and revision_effect.result_changed:
            flags.add(MacroStateQualityFlag.REVISION_CHANGED_RESULT)
        if (
            evaluation_mode is EvaluationMode.PIT_DATE_EOD
            and failure is None
            and historical_coverage is not HistoricalCoverageState.FULL
        ):
            flags.add(MacroStateQualityFlag.HISTORICAL_COVERAGE_LIMITATION)

        is_pit_safe = (
            evaluation_mode is EvaluationMode.PIT_DATE_EOD
            and failure is None
            and len(rows) == 9
            and all(item.is_point_in_time_safe for item in rows)
        )
        payload: dict[str, object] = {
            "state_id": self.composite_catalog.state_id,
            "composite_spec_version": self.composite_catalog.composite_spec_version,
            "composite_spec_hash": self.composite_catalog.specification_hash,
            "evaluation_mode": evaluation_mode.value,
            "evaluation_as_of": evaluation_date.isoformat(),
            "growth": growth.model_dump(mode="json"),
            "inflation": inflation.model_dump(mode="json"),
            "context": [item.model_dump(mode="json") for item in context],
            "historical_coverage_state": historical_coverage.value,
            "macro_state_status": macro_status.value,
            "flags": sorted(item.value for item in flags),
            "ordered_pillar_hashes": [
                by_pillar[pillar_id].calculation_hash
                if pillar_id in by_pillar
                else f"MISSING:{pillar_id}"
                for pillar_id in self._ordered_ids()
            ],
        }
        if evaluation_as_of is not None:
            payload.update(
                revision_provenance_version=(self.revision_provenance.revision_provenance_version),
                revision_provenance_hash=self.revision_provenance.specification_hash,
                revision_effect_hash=(
                    revision_effect.revision_effect_hash if revision_effect is not None else None
                ),
            )
        return MacroStateResult(
            state_id=self.composite_catalog.state_id,
            composite_spec_version=self.composite_catalog.composite_spec_version,
            composite_spec_hash=self.composite_catalog.specification_hash,
            evaluation_mode=evaluation_mode,
            evaluation_as_of=evaluation_date,
            growth=growth,
            inflation=inflation,
            context=context,
            historical_coverage_state=historical_coverage,
            source_registry_hash=self.composite_catalog.required_source_registry_hash,
            feature_spec_version=self.composite_catalog.required_feature_spec_version,
            normalization_spec_version=(self.composite_catalog.required_normalization_spec_version),
            normalization_spec_hash=self.composite_catalog.required_normalization_spec_hash,
            pillar_spec_version=self.composite_catalog.required_pillar_spec_version,
            pillar_spec_hash=self.composite_catalog.required_pillar_spec_hash,
            is_point_in_time_safe=is_pit_safe,
            macro_state_status=macro_status,
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

    async def compute_macro_state(self, evaluation_as_of: date | None = None) -> MacroStateResult:
        """Compute one current or date-EOD state through the Stage 3C boundary."""
        rows = await self.pillar_engine.compute_all_pillars(evaluation_as_of)
        by_pillar = {item.pillar_id: item for item in rows}
        key = self._cache_key(by_pillar, evaluation_as_of)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        result = self._build_result(rows, evaluation_as_of)
        self.cache.put(key, result)
        return result


async def compute_macro_state(
    session: Session, evaluation_as_of: date | None = None
) -> MacroStateResult:
    """Public Stage 3D domain API for the three-layer US macro state."""
    return await MacroStateEngine(session, cache=_PROCESS_CACHE).compute_macro_state(
        evaluation_as_of
    )
