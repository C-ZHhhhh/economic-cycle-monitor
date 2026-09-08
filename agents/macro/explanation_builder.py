"""Pure deterministic snapshot, comparison, and attribution construction."""

from __future__ import annotations

from decimal import Decimal, localcontext

from agents.macro.explanation_config import ExplanationAuthority, hash_payload
from agents.macro.explanation_schemas import (
    AuthorityBindings,
    ChangeType,
    ComparisonResult,
    DependencyAvailability,
    DeterministicDriver,
    DriverAxis,
    DriverRankings,
    EntityType,
    EvidenceItem,
    ExplanationChange,
    FreshnessApplicability,
    LoadedExplanationContext,
    MacroExplanationSnapshot,
    PersistedCadence,
    PersistedExplanationContext,
    RevisionPresentation,
    RevisionStatus,
)
from app.composites.schemas import EvaluationMode, MacroStateResult
from app.pillars.schemas import PillarResult, PillarRole
from app.regimes.schemas import RegimeResult
from app.signals.schemas import NormalizedSignalResult


class ExplanationSnapshotError(RuntimeError):
    """A persisted context violates the approved explanation authority."""


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


class MacroExplanationSnapshotBuilder:
    """Transform typed persisted objects into a canonical snapshot without I/O."""

    def __init__(self, authority: ExplanationAuthority) -> None:
        self.authority = authority

    def _validate_authorities(self, context: PersistedExplanationContext) -> None:
        regime = context.regime
        state = context.macro_state
        authority = self.authority
        checks = {
            "source_registry_hash": (
                regime.source_registry_hash,
                authority.required_source_registry_hash,
            ),
            "feature_spec_version": (
                regime.feature_spec_version,
                authority.required_feature_spec_version,
            ),
            "normalization_spec_version": (
                regime.normalization_spec_version,
                authority.required_normalization_spec_version,
            ),
            "normalization_spec_hash": (
                regime.normalization_spec_hash,
                authority.required_normalization_spec_hash,
            ),
            "pillar_spec_version": (
                regime.pillar_spec_version,
                authority.required_pillar_spec_version,
            ),
            "pillar_spec_hash": (
                regime.pillar_spec_hash,
                authority.required_pillar_spec_hash,
            ),
            "composite_spec_version": (
                regime.stage3d_spec_version,
                authority.required_composite_spec_version,
            ),
            "composite_spec_hash": (
                regime.stage3d_spec_hash,
                authority.required_composite_spec_hash,
            ),
            "regime_spec_version": (
                regime.regime_spec_version,
                authority.required_regime_spec_version,
            ),
            "regime_spec_hash": (
                regime.regime_spec_hash,
                authority.required_regime_spec_hash,
            ),
            "macro_state_hash": (
                regime.macro_state_calculation_hash,
                state.calculation_hash,
            ),
        }
        mismatch = next((key for key, pair in checks.items() if pair[0] != pair[1]), None)
        if mismatch is not None:
            raise ExplanationSnapshotError(f"EXPLANATION_AUTHORITY_MISMATCH:{mismatch}")
        if (
            state.source_registry_hash != authority.required_source_registry_hash
            or state.feature_spec_version != authority.required_feature_spec_version
            or state.normalization_spec_version != authority.required_normalization_spec_version
            or state.normalization_spec_hash != authority.required_normalization_spec_hash
            or state.pillar_spec_version != authority.required_pillar_spec_version
            or state.pillar_spec_hash != authority.required_pillar_spec_hash
            or state.composite_spec_version != authority.required_composite_spec_version
            or state.composite_spec_hash != authority.required_composite_spec_hash
        ):
            raise ExplanationSnapshotError("EXPLANATION_MACRO_STATE_AUTHORITY_MISMATCH")
        pillar_ids = tuple(sorted(item.pillar_id for item in context.pillars))
        if pillar_ids != authority.expected_pillar_ids:
            raise ExplanationSnapshotError("EXPLANATION_PILLAR_INVENTORY_MISMATCH")
        by_pillar = {item.pillar_id: item for item in context.pillars}
        if any(
            item.evaluation_as_of != context.evaluation_as_of
            or item.source_registry_hash != authority.required_source_registry_hash
            or item.feature_spec_version != authority.required_feature_spec_version
            or item.normalization_spec_version != authority.required_normalization_spec_version
            or item.normalization_spec_hash != authority.required_normalization_spec_hash
            or item.pillar_spec_version != authority.required_pillar_spec_version
            or item.pillar_spec_hash != authority.required_pillar_spec_hash
            for item in context.pillars
        ):
            raise ExplanationSnapshotError("EXPLANATION_PILLAR_AUTHORITY_MISMATCH")
        if any(
            item.registry_hash != authority.required_source_registry_hash
            or item.feature_spec_version != authority.required_feature_spec_version
            or item.normalization_spec_version != authority.required_normalization_spec_version
            or item.normalization_spec_hash != authority.required_normalization_spec_hash
            or (
                context.evaluation_mode is EvaluationMode.CURRENT
                and (item.as_of is not None or item.is_point_in_time_safe)
            )
            or (
                context.evaluation_mode is EvaluationMode.PIT_DATE_EOD
                and (item.as_of != context.evaluation_as_of or not item.is_point_in_time_safe)
            )
            for item in context.signals
        ):
            raise ExplanationSnapshotError("EXPLANATION_SIGNAL_AUTHORITY_MISMATCH")
        expected_hashes = {
            **state.growth.pillar_calculation_hashes,
            state.inflation.pillar_id: state.inflation.calculation_hash,
            **{item.pillar_id: item.calculation_hash for item in state.context},
        }
        if any(
            by_pillar[pillar_id].calculation_hash != calculation_hash
            for pillar_id, calculation_hash in expected_hashes.items()
        ):
            raise ExplanationSnapshotError("EXPLANATION_PILLAR_HASH_MISMATCH")
        if context.evaluation_mode is EvaluationMode.CURRENT:
            readiness = context.readiness
            if readiness is None or (
                readiness.registry_hash != authority.required_source_registry_hash
                or readiness.current_freshness_version
                != authority.required_current_freshness_version
                or readiness.current_freshness_hash != authority.required_current_freshness_hash
            ):
                raise ExplanationSnapshotError("EXPLANATION_CURRENT_READINESS_MISMATCH")
        else:
            revision_objects: tuple[
                RegimeResult | MacroStateResult | PillarResult | NormalizedSignalResult, ...
            ] = (
                context.regime,
                context.macro_state,
                *context.pillars,
                *context.signals,
            )
            versions = {
                item.revision_provenance_version
                for item in revision_objects
                if item.revision_provenance_version is not None
            }
            hashes = {
                item.revision_provenance_hash
                for item in revision_objects
                if item.revision_provenance_hash is not None
            }
            if versions and versions != {authority.required_revision_provenance_version}:
                raise ExplanationSnapshotError("EXPLANATION_REVISION_VERSION_MISMATCH")
            if hashes and hashes != {authority.required_revision_provenance_hash}:
                raise ExplanationSnapshotError("EXPLANATION_REVISION_HASH_MISMATCH")

    @staticmethod
    def _driver_inventory(
        context: PersistedExplanationContext,
    ) -> tuple[list[DeterministicDriver], list[DeterministicDriver]]:
        by_signal = {item.signal_id: item for item in context.signals}
        by_pillar = {item.pillar_id: item for item in context.pillars}
        growth_ids = set(context.macro_state.growth.pillar_ids)
        inflation_id = context.macro_state.inflation.pillar_id
        growth: list[DeterministicDriver] = []
        inflation: list[DeterministicDriver] = []
        for pillar_id in sorted(growth_ids | {inflation_id}):
            pillar = by_pillar[pillar_id]
            valid_count = pillar.valid_constituent_count
            if valid_count <= 0:
                continue
            pillar_weight = (
                context.macro_state.growth.pillar_effective_weights[pillar_id]
                if pillar_id in growth_ids
                else Decimal(1)
            )
            constituent_weight = Decimal(1) / Decimal(valid_count)
            for signal_id in sorted(pillar.constituent_signal_ids):
                contribution = pillar.constituent_contributions[signal_id]
                if contribution is None or pillar_weight == 0:
                    continue
                signal = by_signal.get(signal_id)
                expected_hash = pillar.constituent_calculation_hashes[signal_id]
                if (
                    signal is None
                    or expected_hash is None
                    or signal.calculation_hash != expected_hash
                ):
                    raise ExplanationSnapshotError(
                        f"EXPLANATION_SIGNAL_DEPENDENCY_MISMATCH:{signal_id}"
                    )
                with localcontext() as decimal_context:
                    decimal_context.prec = 50
                    impact = pillar_weight * constituent_weight * contribution
                axis = (
                    DriverAxis.GROWTH if pillar_id in growth_ids else DriverAxis.INFLATION_PRESSURE
                )
                driver_id = f"{axis.value}:{signal_id}"
                row = DeterministicDriver(
                    driver_id=driver_id,
                    axis=axis,
                    signal_id=signal_id,
                    indicator_id=signal.indicator_id,
                    pillar_id=pillar_id,
                    contribution=contribution,
                    pillar_effective_weight=pillar_weight,
                    constituent_effective_weight=constituent_weight,
                    attributed_impact=impact,
                    signal_calculation_hash=signal.calculation_hash,
                    pillar_calculation_hash=pillar.calculation_hash,
                    evidence_refs=(f"pillar:{pillar_id}", f"signal:{signal_id}"),
                )
                (growth if axis is DriverAxis.GROWTH else inflation).append(row)
        return growth, inflation

    def _rankings(self, context: PersistedExplanationContext) -> DriverRankings:
        growth, inflation = self._driver_inventory(context)
        full_growth = tuple(
            sorted(growth, key=lambda item: (-abs(item.attributed_impact), item.signal_id))
        )
        full_inflation = tuple(
            sorted(inflation, key=lambda item: (-abs(item.attributed_impact), item.signal_id))
        )
        top_n = self.authority.top_n

        def positive(rows: list[DeterministicDriver]) -> tuple[str, ...]:
            return tuple(
                item.driver_id
                for item in sorted(
                    (item for item in rows if item.attributed_impact > 0),
                    key=lambda item: (-item.attributed_impact, item.signal_id),
                )[:top_n]
            )

        def negative(rows: list[DeterministicDriver]) -> tuple[str, ...]:
            return tuple(
                item.driver_id
                for item in sorted(
                    (item for item in rows if item.attributed_impact < 0),
                    key=lambda item: (item.attributed_impact, item.signal_id),
                )[:top_n]
            )

        return DriverRankings(
            full_growth=full_growth,
            full_inflation=full_inflation,
            top_positive_growth=positive(growth),
            top_negative_growth=negative(growth),
            top_inflationary=positive(inflation),
            top_disinflationary=negative(inflation),
        )

    @staticmethod
    def _evidence(
        context: PersistedExplanationContext,
        drivers: DriverRankings,
        comparisons: tuple[ComparisonResult, ...],
    ) -> tuple[EvidenceItem, ...]:
        items = [
            EvidenceItem(
                evidence_id="macro_state",
                entity_type=EntityType.MACRO_STATE,
                entity_id=context.macro_state.state_id,
                status=context.macro_state.macro_state_status.value,
                calculation_hash=context.macro_state.calculation_hash,
                formatted_value=_decimal_text(context.macro_state.growth.value),
            ),
            EvidenceItem(
                evidence_id="regime",
                entity_type=EntityType.REGIME,
                entity_id=context.regime.base_regime_id or "UNCLASSIFIED",
                status=context.regime.regime_status.value,
                calculation_hash=context.regime.calculation_hash,
                formatted_value=None,
            ),
        ]
        for axis, value, confidence, margin, trajectory in (
            (
                "growth",
                context.regime.growth_strength,
                context.regime.growth_data_confidence,
                context.regime.growth_classification_margin,
                context.regime.growth_trajectory,
            ),
            (
                "inflation",
                context.regime.inflation_pressure,
                context.regime.inflation_data_confidence,
                context.regime.inflation_classification_margin,
                context.regime.inflation_trajectory,
            ),
        ):
            items.extend(
                [
                    EvidenceItem(
                        evidence_id=f"axis:{axis}:value",
                        entity_type=EntityType.MACRO_STATE,
                        entity_id=f"{axis}_value",
                        status="AVAILABLE" if value is not None else "MISSING",
                        calculation_hash=context.regime.calculation_hash,
                        formatted_value=_decimal_text(value),
                    ),
                    EvidenceItem(
                        evidence_id=f"axis:{axis}:confidence",
                        entity_type=EntityType.MACRO_STATE,
                        entity_id=f"{axis}_data_confidence",
                        status="AVAILABLE",
                        calculation_hash=context.regime.calculation_hash,
                        formatted_value=_decimal_text(confidence),
                    ),
                    EvidenceItem(
                        evidence_id=f"axis:{axis}:margin",
                        entity_type=EntityType.REGIME,
                        entity_id=f"{axis}_classification_margin",
                        status="AVAILABLE" if margin is not None else "MISSING",
                        calculation_hash=context.regime.calculation_hash,
                        formatted_value=_decimal_text(margin),
                    ),
                    EvidenceItem(
                        evidence_id=f"trajectory:{axis}",
                        entity_type=EntityType.MACRO_STATE,
                        entity_id=f"{axis}_trajectory",
                        status=(
                            trajectory.qualifier.value
                            if trajectory.qualifier is not None
                            else trajectory.status.value
                        ),
                        calculation_hash=context.regime.calculation_hash,
                        formatted_value=_decimal_text(trajectory.delta),
                    ),
                ]
            )
        for pillar in context.pillars:
            items.append(
                EvidenceItem(
                    evidence_id=f"pillar:{pillar.pillar_id}",
                    entity_type=EntityType.PILLAR,
                    entity_id=pillar.pillar_id,
                    status=pillar.pillar_status.value,
                    calculation_hash=pillar.calculation_hash,
                    formatted_value=_decimal_text(pillar.value),
                )
            )
        for signal in context.signals:
            items.append(
                EvidenceItem(
                    evidence_id=f"signal:{signal.signal_id}",
                    entity_type=EntityType.SIGNAL,
                    entity_id=signal.signal_id,
                    status=signal.signal_status.value,
                    calculation_hash=signal.calculation_hash,
                    formatted_value=_decimal_text(signal.directed_value),
                )
            )
        for driver in (*drivers.full_growth, *drivers.full_inflation):
            items.append(
                EvidenceItem(
                    evidence_id=f"driver:{driver.driver_id}",
                    entity_type=EntityType.DRIVER,
                    entity_id=driver.driver_id,
                    status="AVAILABLE",
                    calculation_hash=hash_payload(driver.model_dump(mode="json")),
                    formatted_value=_decimal_text(driver.attributed_impact),
                )
            )
        if context.readiness is not None:
            items.append(
                EvidenceItem(
                    evidence_id="readiness",
                    entity_type=EntityType.READINESS,
                    entity_id="CURRENT_DATA_READY",
                    status=str(context.readiness.current_data_ready).lower(),
                    calculation_hash=context.readiness.current_freshness_hash,
                    formatted_value=None,
                )
            )
        for health in context.health:
            entity_id = health.indicator_id or health.source
            items.append(
                EvidenceItem(
                    evidence_id=f"health:{entity_id}:{health.source}",
                    entity_type=EntityType.HEALTH,
                    entity_id=entity_id,
                    status=health.runtime_health.value,
                    calculation_hash=health.current_freshness_hash,
                    formatted_value=None,
                )
            )
        for comparison in comparisons:
            for change in comparison.changes:
                evidence_id = (
                    f"change:{comparison.cadence.value}:"
                    f"{change.entity_type.value}:{change.entity_id}"
                )
                items.append(
                    EvidenceItem(
                        evidence_id=evidence_id,
                        entity_type=EntityType.CHANGE,
                        entity_id=change.entity_id,
                        status=change.change_type.value,
                        calculation_hash=hash_payload(change.model_dump(mode="json")),
                        formatted_value=_decimal_text(change.delta),
                    )
                )
        return tuple(sorted(items, key=lambda item: item.evidence_id))

    def _comparison(
        self,
        current: PersistedExplanationContext,
        previous: PersistedExplanationContext | None,
        *,
        status: DependencyAvailability,
        cadence: PersistedCadence,
    ) -> ComparisonResult:
        if previous is None:
            return ComparisonResult(status=status, cadence=cadence)
        current_drivers = {
            item.driver_id: item for item in sum(self._driver_inventory(current), [])
        }
        previous_drivers = {
            item.driver_id: item for item in sum(self._driver_inventory(previous), [])
        }
        changes: list[ExplanationChange] = []

        def numeric_change(
            entity_type: EntityType,
            entity_id: str,
            prior: Decimal | None,
            now: Decimal | None,
            prior_status: str,
            now_status: str,
            prior_hash: str,
            now_hash: str,
            refs: tuple[str, ...],
        ) -> None:
            if prior is None or now is None:
                kind = ChangeType.NOT_COMPARABLE
                delta = None
            elif prior_status != now_status:
                kind = ChangeType.STATUS_CHANGE
                delta = now - prior
            else:
                kind = ChangeType.VALUE_CHANGE
                delta = now - prior
            changes.append(
                ExplanationChange(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    previous_value=prior,
                    current_value=now,
                    previous_status=prior_status,
                    current_status=now_status,
                    previous_hash=prior_hash,
                    current_hash=now_hash,
                    delta=delta,
                    change_type=kind,
                    evidence_refs=refs,
                )
            )

        numeric_change(
            EntityType.MACRO_STATE,
            current.macro_state.growth.component_id,
            previous.macro_state.growth.value,
            current.macro_state.growth.value,
            previous.macro_state.growth.status.value,
            current.macro_state.growth.status.value,
            previous.macro_state.calculation_hash,
            current.macro_state.calculation_hash,
            ("macro_state",),
        )
        numeric_change(
            EntityType.PILLAR,
            current.macro_state.inflation.pillar_id,
            previous.macro_state.inflation.value,
            current.macro_state.inflation.value,
            previous.macro_state.inflation.status.value,
            current.macro_state.inflation.status.value,
            previous.macro_state.inflation.calculation_hash,
            current.macro_state.inflation.calculation_hash,
            (f"pillar:{current.macro_state.inflation.pillar_id}",),
        )
        prior_pillars = {item.pillar_id: item for item in previous.pillars}
        for pillar in current.pillars:
            if pillar.pillar_id == current.macro_state.inflation.pillar_id:
                continue
            prior = prior_pillars.get(pillar.pillar_id)
            if prior is None:
                continue
            numeric_change(
                EntityType.PILLAR,
                pillar.pillar_id,
                prior.value,
                pillar.value,
                prior.pillar_status.value,
                pillar.pillar_status.value,
                prior.calculation_hash,
                pillar.calculation_hash,
                (f"pillar:{pillar.pillar_id}",),
            )
        for driver_id in sorted(set(current_drivers) | set(previous_drivers)):
            now = current_drivers.get(driver_id)
            prior_driver = previous_drivers.get(driver_id)
            changes.append(
                ExplanationChange(
                    entity_type=EntityType.DRIVER,
                    entity_id=driver_id,
                    previous_value=(prior_driver.attributed_impact if prior_driver else None),
                    current_value=now.attributed_impact if now else None,
                    previous_status="AVAILABLE" if prior_driver else "MISSING",
                    current_status="AVAILABLE" if now else "MISSING",
                    previous_hash=(prior_driver.signal_calculation_hash if prior_driver else None),
                    current_hash=now.signal_calculation_hash if now else None,
                    delta=(
                        now.attributed_impact - prior_driver.attributed_impact
                        if now is not None and prior_driver is not None
                        else None
                    ),
                    change_type=(
                        ChangeType.NEW_INFORMATION
                        if now is not None and prior_driver is None
                        else ChangeType.NOT_COMPARABLE
                        if now is None or prior_driver is None
                        else ChangeType.VALUE_CHANGE
                    ),
                    evidence_refs=(f"driver:{driver_id}",) if now is not None else (),
                )
            )
        if current.regime.base_regime_id != previous.regime.base_regime_id:
            changes.append(
                ExplanationChange(
                    entity_type=EntityType.REGIME,
                    entity_id=current.regime.base_regime_id or "UNCLASSIFIED",
                    previous_status=previous.regime.base_regime_id,
                    current_status=current.regime.base_regime_id,
                    previous_hash=previous.regime.calculation_hash,
                    current_hash=current.regime.calculation_hash,
                    change_type=ChangeType.CLASSIFICATION_CHANGE,
                    evidence_refs=("regime",),
                )
            )
        for axis, current_trajectory, previous_trajectory in (
            ("growth", current.regime.growth_trajectory, previous.regime.growth_trajectory),
            (
                "inflation",
                current.regime.inflation_trajectory,
                previous.regime.inflation_trajectory,
            ),
        ):
            if current_trajectory.qualifier != previous_trajectory.qualifier:
                changes.append(
                    ExplanationChange(
                        entity_type=EntityType.MACRO_STATE,
                        entity_id=f"{axis}_trajectory",
                        previous_status=(
                            previous_trajectory.qualifier.value
                            if previous_trajectory.qualifier is not None
                            else previous_trajectory.status.value
                        ),
                        current_status=(
                            current_trajectory.qualifier.value
                            if current_trajectory.qualifier is not None
                            else current_trajectory.status.value
                        ),
                        previous_hash=previous.regime.calculation_hash,
                        current_hash=current.regime.calculation_hash,
                        change_type=ChangeType.TRAJECTORY_CHANGE,
                        evidence_refs=("regime",),
                    )
                )
        ordered = tuple(sorted(changes, key=lambda item: (item.entity_type.value, item.entity_id)))
        previous_hash = hash_payload(
            {
                "context_hash": previous.persisted_context_hash,
                "regime_hash": previous.regime.calculation_hash,
            }
        )
        return ComparisonResult(
            status=DependencyAvailability.AVAILABLE,
            cadence=cadence,
            previous_evaluation_as_of=previous.evaluation_as_of,
            previous_snapshot_hash=previous_hash,
            changes=ordered,
        )

    def build(self, loaded: LoadedExplanationContext) -> MacroExplanationSnapshot:
        """Build the canonical snapshot; raise instead of fetching missing current input."""
        if loaded.status is not DependencyAvailability.AVAILABLE or loaded.current is None:
            raise ExplanationSnapshotError(loaded.status.value)
        current = loaded.current
        self._validate_authorities(current)
        for comparison in (loaded.previous_release_eod, loaded.previous_month_end):
            if comparison is not None:
                self._validate_authorities(comparison)
        drivers = self._rankings(current)
        release_comparison = self._comparison(
            current,
            loaded.previous_release_eod,
            status=loaded.previous_release_eod_status,
            cadence=PersistedCadence.RELEASE_EVENT_EOD,
        )
        month_end_comparison = self._comparison(
            current,
            loaded.previous_month_end,
            status=loaded.previous_month_end_status,
            cadence=PersistedCadence.MONTH_END_EOD,
        )
        evidence = self._evidence(
            current,
            drivers,
            (release_comparison, month_end_comparison),
        )
        regime = current.regime
        state = current.macro_state
        pillars = tuple(sorted(current.pillars, key=lambda item: item.pillar_id))
        current_mode = current.evaluation_mode is EvaluationMode.CURRENT
        revision_objects: tuple[
            RegimeResult | MacroStateResult | PillarResult | NormalizedSignalResult, ...
        ] = (regime, state, *pillars, *current.signals)
        revision_hashes = tuple(
            sorted(
                {
                    evidence_hash
                    for item in revision_objects
                    for evidence_hash in item.revision_evidence_hashes
                }
            )
        )
        revision_changed = any(
            item.revision_effect is not None and item.revision_effect.result_changed
            for item in revision_objects
        )
        revision = (
            RevisionPresentation(
                status=RevisionStatus.NOT_AVAILABLE_FOR_CURRENT_MODE,
                note=(
                    "Current revision attribution is not available under the approved v1 contract."
                ),
            )
            if current_mode
            else RevisionPresentation(
                status=(
                    RevisionStatus.AVAILABLE
                    if revision_hashes
                    else RevisionStatus.NO_REVISION_EVIDENCE
                ),
                evidence_hashes=revision_hashes,
                result_changed=revision_changed if revision_hashes else None,
                note=(
                    "Canonical revision-provenance-v2 evidence is attached."
                    if revision_hashes
                    else "No canonical revision evidence is present for this PIT result."
                ),
            )
        )
        warnings: set[str] = {
            *(flag.value for flag in regime.quality_flags),
            *(flag.value for flag in state.quality_flags),
            *(
                f"{pillar.pillar_id}:{flag.value}"
                for pillar in pillars
                for flag in pillar.quality_flags
            ),
        }
        if loaded.trajectory_context_status is not DependencyAvailability.AVAILABLE:
            warnings.add(loaded.trajectory_context_status.value)
        if loaded.previous_release_eod is None:
            warnings.add("PREVIOUS_RELEASE_EOD_NOT_COMPARABLE")
        if loaded.previous_month_end is None:
            warnings.add("PREVIOUS_MONTH_END_NOT_COMPARABLE")
        if current.readiness is not None:
            warnings.update(current.readiness.current_blockers)
            warnings.update(current.readiness.current_freshness_warnings)
            warnings.update(current.readiness.degraded_nonblocking)
        authorities = AuthorityBindings(
            source_registry_version=self.authority.required_source_registry_version,
            source_registry_hash=self.authority.required_source_registry_hash,
            current_freshness_version=(
                self.authority.required_current_freshness_version if current_mode else None
            ),
            current_freshness_hash=(
                self.authority.required_current_freshness_hash if current_mode else None
            ),
            feature_spec_version=self.authority.required_feature_spec_version,
            feature_spec_hash=self.authority.required_feature_spec_hash,
            normalization_spec_version=self.authority.required_normalization_spec_version,
            normalization_spec_hash=self.authority.required_normalization_spec_hash,
            pillar_spec_version=self.authority.required_pillar_spec_version,
            pillar_spec_hash=self.authority.required_pillar_spec_hash,
            composite_spec_version=self.authority.required_composite_spec_version,
            composite_spec_hash=self.authority.required_composite_spec_hash,
            regime_spec_version=self.authority.required_regime_spec_version,
            regime_spec_hash=self.authority.required_regime_spec_hash,
            pit_materialization_version=(
                self.authority.required_pit_materialization_version if not current_mode else None
            ),
            pit_materialization_hash=(
                self.authority.required_pit_materialization_hash if not current_mode else None
            ),
            revision_provenance_version=(
                self.authority.required_revision_provenance_version if not current_mode else None
            ),
            revision_provenance_hash=(
                self.authority.required_revision_provenance_hash if not current_mode else None
            ),
        )
        payload = {
            "snapshot_spec_hash": self.authority.specification_hash,
            "snapshot_calculation_hash": "pending",
            "evaluation_mode": current.evaluation_mode,
            "evaluation_as_of": current.evaluation_as_of,
            "readiness": current.readiness if current_mode else None,
            "freshness_applicability": (
                FreshnessApplicability.CURRENT
                if current_mode
                else FreshnessApplicability.NOT_APPLICABLE
            ),
            "current_health": tuple(
                sorted(
                    current.health if current_mode else (),
                    key=lambda item: (item.indicator_id or "", item.source),
                )
            ),
            "growth_strength": regime.growth_strength,
            "inflation_pressure": regime.inflation_pressure,
            "growth_condition": regime.growth_condition,
            "inflation_condition": regime.inflation_condition,
            "base_regime_id": regime.base_regime_id,
            "growth_trajectory": regime.growth_trajectory.qualifier,
            "inflation_trajectory": regime.inflation_trajectory.qualifier,
            "growth_data_confidence": regime.growth_data_confidence,
            "inflation_data_confidence": regime.inflation_data_confidence,
            "minimum_axis_confidence": regime.minimum_axis_data_confidence,
            "growth_classification_margin": regime.growth_classification_margin,
            "inflation_classification_margin": regime.inflation_classification_margin,
            "macro_state_status": regime.macro_state_status,
            "regime_status": regime.regime_status,
            "pillars": pillars,
            "drivers": drivers,
            "contextual_diagnostics": tuple(
                f"{pillar.pillar_id}:{pillar.pillar_status.value}:{_decimal_text(pillar.value)}"
                for pillar in pillars
                if pillar.pillar_role is PillarRole.CONTEXTUAL
            ),
            "previous_release_eod": release_comparison,
            "previous_month_end": month_end_comparison,
            "revision": revision,
            "warnings": tuple(sorted(warnings)),
            "authorities": authorities,
            "upstream_calculation_hashes": tuple(
                sorted(
                    {
                        regime.calculation_hash,
                        state.calculation_hash,
                        *(item.calculation_hash for item in pillars),
                        *(item.calculation_hash for item in current.signals),
                    }
                )
            ),
            "evidence_registry": evidence,
        }
        pending = MacroExplanationSnapshot.model_validate(payload)
        calculation_hash = hash_payload(
            pending.model_dump(mode="json", exclude={"snapshot_calculation_hash"})
        )
        return pending.model_copy(update={"snapshot_calculation_hash": calculation_hash})
