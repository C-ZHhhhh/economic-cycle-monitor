"""Compute-on-demand Stage 3B signals exclusively from Stage 3A features."""

import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Sequence
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.features.config import load_feature_catalog
from app.features.formulas import expected_input_dates, shift_period
from app.features.schemas import FeatureCatalog, FeatureResult, FeatureStatus
from app.features.service import FeatureEngine
from app.signals.cache import SignalCacheKey, SignalResultCache
from app.signals.config import load_signal_catalog
from app.signals.normalizers import DISPERSION_ESTIMATOR, rolling_zscore
from app.signals.schemas import (
    Direction,
    NormalizedSignalResult,
    SignalCatalog,
    SignalDefinition,
    SignalQualityFlag,
    SignalStatus,
)
from app.source_registry.config import load_source_registry
from app.source_registry.materialization import load_pit_materialization_catalog
from app.source_registry.permanent_gaps import PermanentGapCatalog, load_permanent_gap_catalog
from app.source_registry.revision_provenance import (
    RevisionProvenanceCatalog,
    build_layer_effect,
    load_revision_provenance_catalog,
)
from app.source_registry.revision_schemas import RevisionEffectType, RevisionLayerEffect
from app.source_registry.schemas import SourceRegistryCatalog

_PROCESS_CACHE = SignalResultCache(max_entries=256)


class FeatureService(Protocol):
    """Minimal Stage 3A interface accepted by the Stage 3B service."""

    def compute_feature(
        self,
        feature_id: str,
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> Awaitable[list[FeatureResult]]:
        """Return one configured Stage 3A feature series."""
        ...


class SignalEngineError(RuntimeError):
    """Base fail-closed Stage 3B service error."""


class SignalNotFound(SignalEngineError):
    """A stable signal or feature is absent from the specification."""


class SignalContractMismatch(SignalEngineError):
    """Signal, feature, and source-registry authorities do not match."""


def _hash_payload(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _feature_hash(rows: Sequence[FeatureResult]) -> str:
    return _hash_payload(
        [
            {
                "feature_id": item.feature_id,
                "date": item.feature_observation_date.isoformat(),
                "as_of": item.as_of.isoformat() if item.as_of else "CURRENT",
                "status": item.feature_status.value,
                "calculation_hash": item.calculation_hash,
            }
            for item in rows
        ]
    )


def _propagated_flags(rows: Sequence[FeatureResult]) -> set[SignalQualityFlag]:
    flags: set[SignalQualityFlag] = set()
    for row in rows:
        for flag in row.quality_flags:
            if flag.value == SignalQualityFlag.REVISION_CHANGED_RESULT.value:
                continue
            try:
                flags.add(SignalQualityFlag(flag.value))
            except ValueError:
                continue
    return flags


def _counterfactual_feature(row: FeatureResult) -> tuple[Decimal | None, FeatureStatus]:
    """Return the canonical predecessor value/status carried by a Stage 3A result."""
    effect = row.revision_effect
    if effect is None:
        return row.value, row.feature_status
    try:
        status = FeatureStatus(effect.counterfactual_status)
    except ValueError as error:
        raise SignalEngineError("INVALID_FEATURE_REVISION_STATUS") from error
    return effect.counterfactual_value, status


def _signal_revision_effect(
    *,
    definition: SignalDefinition,
    target: FeatureResult,
    history: list[FeatureResult],
    current_status: SignalStatus,
    current_directed: Decimal | None,
    current_center: Decimal | None,
    current_scale: Decimal | None,
    authority: RevisionProvenanceCatalog,
    extreme_value_threshold: Decimal,
    low_dispersion_relative_threshold: Decimal,
) -> RevisionLayerEffect | None:
    """Compare a signal with the same result under canonical predecessor feature values."""
    involved = [*history, target]
    effects = [item.revision_effect for item in involved if item.revision_effect is not None]
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
    target_value, target_status = _counterfactual_feature(target)
    counterfactual_history = [_counterfactual_feature(item) for item in history]
    counterfactual_status = SignalStatus.FEATURE_ERROR
    counterfactual_directed: Decimal | None = None
    counterfactual_center: Decimal | None = None
    counterfactual_scale: Decimal | None = None
    if target_status is FeatureStatus.PIT_UNSAFE or any(
        status is FeatureStatus.PIT_UNSAFE for _, status in counterfactual_history
    ):
        counterfactual_status = SignalStatus.PIT_UNSAFE
    elif (
        target_status is FeatureStatus.OK
        and target_value is not None
        and all(
            status is FeatureStatus.OK and value is not None
            for value, status in counterfactual_history
        )
    ):
        outcome = rolling_zscore(
            target_value,
            [value for value, _ in counterfactual_history if value is not None],
            full_window=definition.full_window,
            min_history=definition.min_history,
            extreme_value_threshold=extreme_value_threshold,
            low_dispersion_relative_threshold=low_dispersion_relative_threshold,
        )
        counterfactual_status = outcome.status
        counterfactual_center = outcome.center
        counterfactual_scale = outcome.scale
        if (
            outcome.status is SignalStatus.OK
            and outcome.normalized_value is not None
            and definition.direction_multiplier is not None
        ):
            with localcontext() as context:
                context.prec = 34
                counterfactual_directed = outcome.normalized_value * definition.direction_multiplier
    distribution_changed = (
        current_center != counterfactual_center or current_scale != counterfactual_scale
    ) and any(
        item
        in {
            RevisionEffectType.SOURCE_REVISION,
            RevisionEffectType.OBSERVATION_REMOVAL,
            RevisionEffectType.OBSERVATION_STATUS_CHANGE,
        }
        for item in source_effects
    )
    return build_layer_effect(
        authority=authority,
        source_evidence_hashes=evidence_hashes,
        source_effect_types=source_effects,
        current_value=current_directed,
        counterfactual_value=counterfactual_directed,
        current_status=current_status.value,
        counterfactual_status=counterfactual_status.value,
        distribution_changed=distribution_changed,
    )


class SignalEngine:
    """Normalize Stage 3A features with strict prior-only trailing history."""

    def __init__(
        self,
        session: Session,
        *,
        source_registry: SourceRegistryCatalog | None = None,
        feature_catalog: FeatureCatalog | None = None,
        signal_catalog: SignalCatalog | None = None,
        feature_engine: FeatureService | None = None,
        cache: SignalResultCache | None = None,
        permanent_gaps: PermanentGapCatalog | None = None,
    ) -> None:
        self.session = session
        self.source_registry = source_registry or load_source_registry()
        self.feature_catalog = feature_catalog or load_feature_catalog(
            source_registry=self.source_registry
        )
        self.signal_catalog = signal_catalog or load_signal_catalog(
            source_registry=self.source_registry,
            feature_catalog=self.feature_catalog,
        )
        if (
            self.signal_catalog.required_feature_spec_version
            != self.feature_catalog.feature_spec_version
            or self.signal_catalog.required_source_registry_version
            != self.source_registry.registry_policy_version
            or self.signal_catalog.required_source_registry_hash
            != self.source_registry.registry_hash
        ):
            raise SignalContractMismatch("SIGNAL_CONTRACT_MISMATCH")
        self.feature_engine = feature_engine or FeatureEngine(
            session,
            source_registry=self.source_registry,
            feature_catalog=self.feature_catalog,
        )
        self.cache = cache if cache is not None else SignalResultCache()
        self.permanent_gaps = permanent_gaps or load_permanent_gap_catalog(
            source_registry=self.source_registry
        )
        materialization = load_pit_materialization_catalog(source_registry=self.source_registry)
        self.revision_provenance = load_revision_provenance_catalog(
            source_registry=self.source_registry,
            materialization=materialization,
        )

    @staticmethod
    def _blocked_only_by_permanent_gap(
        row: FeatureResult, gap_periods: frozenset[date], *, frequency: str
    ) -> bool:
        """Return whether a failed history feature lacks only declared permanent gaps."""
        if row.feature_status is not FeatureStatus.MISSING_INPUT_PERIOD:
            return False
        expected = expected_input_dates(
            row.formula_id, row.feature_observation_date, frequency, row.window
        )
        missing = set(expected) - set(row.input_observation_dates)
        return bool(missing) and missing <= gap_periods

    def _history_for_target(
        self,
        definition: SignalDefinition,
        target: FeatureResult,
        by_date: dict[date, FeatureResult],
        *,
        frequency: str,
        as_of: date | None,
    ) -> tuple[list[FeatureResult], SignalStatus | None, list[date]]:
        """Collect the strict trailing window, skipping only declared permanent gaps.

        A period the official source confirmed it will never publish cannot be
        waited for, so it is left out of the distribution instead of failing the
        signal for the whole window. Nothing is imputed, every other missing or
        failed period still fails closed, and the skipped periods are returned so
        the result can disclose them.
        """
        expected = [
            shift_period(target.feature_observation_date, frequency, offset)
            for offset in range(-definition.full_window, 0)
        ]
        gap_periods = self.permanent_gaps.periods_for(definition.indicator_id, as_of=as_of)
        earliest = min(by_date) if by_date else None
        history: list[FeatureResult] = []
        skipped: list[date] = []
        for expected_date in expected:
            row = by_date.get(expected_date)
            if row is None:
                if expected_date in gap_periods:
                    skipped.append(expected_date)
                    continue
                if earliest is not None and expected_date >= earliest:
                    return history, SignalStatus.MISSING_FEATURE, skipped
                continue
            if row.feature_status is not FeatureStatus.OK or row.value is None:
                if row.feature_status is FeatureStatus.PIT_UNSAFE:
                    return history, SignalStatus.PIT_UNSAFE, skipped
                if self._blocked_only_by_permanent_gap(row, gap_periods, frequency=frequency):
                    skipped.append(expected_date)
                    continue
                return history, SignalStatus.FEATURE_ERROR, skipped
            history.append(row)
        return history, None, skipped

    def _result(
        self,
        definition: SignalDefinition,
        target: FeatureResult,
        history: list[FeatureResult],
        *,
        forced_status: SignalStatus | None,
        as_of: date | None,
        skipped_gap_periods: Sequence[date] = (),
    ) -> NormalizedSignalResult:
        involved = [*history, target]
        flags = _propagated_flags(involved)
        if skipped_gap_periods:
            flags.add(SignalQualityFlag.PERMANENT_GAP_SKIPPED)
        status = forced_status
        if target.feature_spec_version != self.feature_catalog.feature_spec_version:
            status = SignalStatus.FEATURE_SPEC_MISMATCH
        elif target.registry_hash != self.source_registry.registry_hash:
            status = SignalStatus.REGISTRY_HASH_MISMATCH
        elif target.feature_status is FeatureStatus.PIT_UNSAFE:
            status = SignalStatus.PIT_UNSAFE
        elif target.feature_status is not FeatureStatus.OK or target.value is None:
            status = SignalStatus.FEATURE_ERROR
        elif as_of is not None and not all(item.is_point_in_time_safe for item in involved):
            status = SignalStatus.PIT_UNSAFE

        outcome = None
        if status is None and target.value is not None:
            outcome = rolling_zscore(
                target.value,
                [item.value for item in history if item.value is not None],
                full_window=definition.full_window,
                min_history=definition.min_history,
                extreme_value_threshold=self.signal_catalog.extreme_value_threshold,
                low_dispersion_relative_threshold=(
                    self.signal_catalog.low_dispersion_relative_threshold
                ),
            )
            status = outcome.status
            flags.update(outcome.quality_flags)

        normalized = outcome.normalized_value if outcome is not None else None
        center = outcome.center if outcome is not None else None
        scale = outcome.scale if outcome is not None else None
        directed: Decimal | None = None
        if status is SignalStatus.OK and definition.direction is Direction.NEUTRAL_CONTEXT_ONLY:
            flags.add(SignalQualityFlag.CONTEXT_ONLY)
        elif (
            status is SignalStatus.OK
            and normalized is not None
            and definition.direction_multiplier is not None
        ):
            with localcontext() as context:
                context.prec = 34
                directed = normalized * definition.direction_multiplier

        revision_effect = _signal_revision_effect(
            definition=definition,
            target=target,
            history=history,
            current_status=status or SignalStatus.FEATURE_ERROR,
            current_directed=directed if status is SignalStatus.OK else None,
            current_center=center,
            current_scale=scale,
            authority=self.revision_provenance,
            extreme_value_threshold=self.signal_catalog.extreme_value_threshold,
            low_dispersion_relative_threshold=(
                self.signal_catalog.low_dispersion_relative_threshold
            ),
        )
        if revision_effect is not None and revision_effect.result_changed:
            flags.add(SignalQualityFlag.REVISION_CHANGED_RESULT)
        if revision_effect is not None and revision_effect.distribution_changed:
            flags.add(SignalQualityFlag.REVISION_CHANGED_DISTRIBUTION)

        history_hashes = [item.calculation_hash for item in history]
        history_hash = _hash_payload(history_hashes)
        input_hashes = [*history_hashes, target.calculation_hash]
        payload: dict[str, Any] = {
            "signal_specification_hash": self.signal_catalog.specification_hash,
            "signal_id": definition.signal_id,
            "target_feature_hash": target.calculation_hash,
            "history_feature_hash": history_hash,
            "as_of": as_of.isoformat() if as_of else "CURRENT",
            "center": str(center) if center is not None else None,
            "scale": str(scale) if scale is not None else None,
            "normalized": str(normalized) if normalized is not None else None,
            "directed": str(directed) if directed is not None else None,
            "status": status.value if status is not None else SignalStatus.FEATURE_ERROR.value,
            "quality_flags": sorted(item.value for item in flags),
        }
        if as_of is not None:
            payload.update(
                revision_provenance_version=(self.revision_provenance.revision_provenance_version),
                revision_provenance_hash=self.revision_provenance.specification_hash,
                revision_effect_hash=(
                    revision_effect.revision_effect_hash if revision_effect is not None else None
                ),
            )
        if skipped_gap_periods:
            # Bound only when the rule was applied, so every result that never met a
            # declared gap keeps its existing calculation identity.
            payload.update(
                permanent_gap_version=self.permanent_gaps.permanent_gap_version,
                permanent_gap_hash=self.permanent_gaps.permanent_gap_hash,
                permanent_gap_periods=[item.isoformat() for item in skipped_gap_periods],
            )
        result = NormalizedSignalResult(
            signal_id=definition.signal_id,
            feature_id=definition.feature_id,
            feature_spec_version=self.feature_catalog.feature_spec_version,
            normalization_spec_version=self.signal_catalog.normalization_spec_version,
            normalization_spec_hash=self.signal_catalog.specification_hash,
            indicator_id=definition.indicator_id,
            economic_axis=definition.economic_axis,
            feature_role=definition.feature_role,
            redundancy_group=definition.redundancy_group,
            feature_observation_date=target.feature_observation_date,
            as_of=as_of,
            raw_feature_value=target.value,
            raw_feature_unit=target.unit,
            normalized_value=normalized if status is SignalStatus.OK else None,
            directed_value=directed if status is SignalStatus.OK else None,
            normalized_unit=definition.normalized_unit,
            normalization_method=definition.normalization_method,
            window=definition.full_window,
            min_history=definition.min_history,
            history_start=history[0].feature_observation_date if history else None,
            history_end=history[-1].feature_observation_date if history else None,
            history_count=len(history),
            window_coverage=Decimal(len(history)) / Decimal(definition.full_window),
            center=center,
            scale=scale,
            dispersion_estimator=DISPERSION_ESTIMATOR,
            distribution_includes_current=False,
            direction=definition.direction,
            direction_multiplier=definition.direction_multiplier,
            source_policy_version=target.source_policy_version,
            registry_hash=self.source_registry.registry_hash,
            feature_calculation_hash=target.calculation_hash,
            input_feature_calculation_hashes=input_hashes,
            history_feature_calculation_hash=history_hash,
            is_point_in_time_safe=(
                as_of is not None
                and bool(involved)
                and all(item.is_point_in_time_safe for item in involved)
            ),
            signal_status=status or SignalStatus.FEATURE_ERROR,
            quality_flags=sorted(flags, key=str),
            calculation_hash=_hash_payload(payload),
            revision_provenance_version=(
                self.revision_provenance.revision_provenance_version if as_of is not None else None
            ),
            revision_provenance_hash=(
                self.revision_provenance.specification_hash if as_of is not None else None
            ),
            revision_evidence_hashes=(
                revision_effect.source_revision_evidence_hashes
                if revision_effect is not None
                else ()
            ),
            revision_effect=revision_effect,
        )
        return result

    async def _compute_definition(
        self,
        definition: SignalDefinition,
        *,
        start: date,
        end: date,
        as_of: date | datetime | None,
    ) -> list[NormalizedSignalResult]:
        if start > end:
            raise ValueError("start must not be after end")
        if isinstance(as_of, datetime):
            raise ValueError("PIT_GRANULARITY_UNSUPPORTED")
        indicator, _ = self.feature_catalog.by_feature()[definition.feature_id]
        frequency = self.source_registry.by_indicator()[indicator.indicator_id].canonical.frequency
        history_start = shift_period(start, frequency, -definition.full_window)
        feature_rows = sorted(
            await self.feature_engine.compute_feature(
                definition.feature_id, history_start, end, as_of
            ),
            key=lambda item: item.feature_observation_date,
        )
        date_as_of = as_of if isinstance(as_of, date) and not isinstance(as_of, datetime) else None
        requested_rows = [
            item for item in feature_rows if start <= item.feature_observation_date <= end
        ]
        prior_rows = [item for item in feature_rows if item.feature_observation_date < start]
        cache_key = SignalCacheKey(
            signal_id=definition.signal_id,
            normalization_spec_version=self.signal_catalog.normalization_spec_version,
            normalization_spec_hash=self.signal_catalog.specification_hash,
            feature_spec_version=self.feature_catalog.feature_spec_version,
            start=start,
            end=end,
            as_of=date_as_of,
            registry_hash=self.source_registry.registry_hash,
            input_feature_calculation_hash=_feature_hash(requested_rows),
            history_feature_calculation_hash=_feature_hash(prior_rows),
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        by_date = {item.feature_observation_date: item for item in feature_rows}
        results: list[NormalizedSignalResult] = []
        for target in requested_rows:
            history, forced_status, skipped = self._history_for_target(
                definition, target, by_date, frequency=frequency, as_of=date_as_of
            )
            results.append(
                self._result(
                    definition,
                    target,
                    history,
                    forced_status=forced_status,
                    as_of=date_as_of,
                    skipped_gap_periods=skipped,
                )
            )
        self.cache.put(cache_key, results)
        return results

    async def compute_signal(
        self,
        signal_id: str,
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> list[NormalizedSignalResult]:
        """Compute one stable signal over an inclusive native-period range."""
        try:
            definition = self.signal_catalog.by_signal()[signal_id]
        except KeyError as error:
            raise SignalNotFound(f"UNKNOWN_SIGNAL:{signal_id}") from error
        return await self._compute_definition(definition, start=start, end=end, as_of=as_of)

    async def compute_feature_signals(
        self,
        feature_id: str,
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> list[NormalizedSignalResult]:
        """Compute the exactly-one Stage 3B-v1 signal mapped from a feature."""
        try:
            definition = self.signal_catalog.by_feature()[feature_id]
        except KeyError as error:
            raise SignalNotFound(f"UNKNOWN_FEATURE:{feature_id}") from error
        return await self._compute_definition(definition, start=start, end=end, as_of=as_of)

    async def compute_signal_set(
        self,
        signal_ids: Sequence[str],
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> list[NormalizedSignalResult]:
        """Compute selected one-to-one signals without duplicate feature fetches."""
        definitions = self.signal_catalog.by_signal()
        requested: OrderedDict[str, SignalDefinition] = OrderedDict()
        for signal_id in signal_ids:
            try:
                definition = definitions[signal_id]
            except KeyError as error:
                raise SignalNotFound(f"UNKNOWN_SIGNAL:{signal_id}") from error
            requested.setdefault(definition.feature_id, definition)
        results: list[NormalizedSignalResult] = []
        for definition in requested.values():
            results.extend(
                await self._compute_definition(definition, start=start, end=end, as_of=as_of)
            )
        return results


async def compute_signal(
    session: Session,
    signal_id: str,
    start: date,
    end: date,
    as_of: date | datetime | None = None,
) -> list[NormalizedSignalResult]:
    """Public domain API for one configured Stage 3B signal."""
    return await SignalEngine(session, cache=_PROCESS_CACHE).compute_signal(
        signal_id, start, end, as_of
    )


async def compute_feature_signals(
    session: Session,
    feature_id: str,
    start: date,
    end: date,
    as_of: date | datetime | None = None,
) -> list[NormalizedSignalResult]:
    """Public domain API for the signal mapped from one Stage 3A feature."""
    return await SignalEngine(session, cache=_PROCESS_CACHE).compute_feature_signals(
        feature_id, start, end, as_of
    )


async def compute_signal_set(
    session: Session,
    signal_ids: Sequence[str],
    start: date,
    end: date,
    as_of: date | datetime | None = None,
) -> list[NormalizedSignalResult]:
    """Public domain API for selected configured Stage 3B signals."""
    return await SignalEngine(session, cache=_PROCESS_CACHE).compute_signal_set(
        signal_ids, start, end, as_of
    )
