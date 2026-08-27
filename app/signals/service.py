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
from app.features.formulas import shift_period
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
            try:
                flags.add(SignalQualityFlag(flag.value))
            except ValueError:
                continue
    return flags


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
        self._revision_distributions: OrderedDict[
            tuple[str, date], tuple[Decimal | None, Decimal | None]
        ] = OrderedDict()

    def _remember_revision(
        self,
        result: NormalizedSignalResult,
        *,
        max_entries: int = 512,
    ) -> NormalizedSignalResult:
        key = (result.signal_id, result.feature_observation_date)
        current = (result.center, result.scale)
        previous = self._revision_distributions.get(key)
        flags = set(result.quality_flags)
        if previous is not None and previous != current:
            flags.add(SignalQualityFlag.REVISION_CHANGED_DISTRIBUTION)
        self._revision_distributions[key] = current
        self._revision_distributions.move_to_end(key)
        while len(self._revision_distributions) > max_entries:
            self._revision_distributions.popitem(last=False)
        return result.model_copy(update={"quality_flags": sorted(flags, key=str)})

    def _history_for_target(
        self,
        definition: SignalDefinition,
        target: FeatureResult,
        by_date: dict[date, FeatureResult],
        *,
        frequency: str,
    ) -> tuple[list[FeatureResult], SignalStatus | None]:
        expected = [
            shift_period(target.feature_observation_date, frequency, offset)
            for offset in range(-definition.full_window, 0)
        ]
        earliest = min(by_date) if by_date else None
        history: list[FeatureResult] = []
        for expected_date in expected:
            row = by_date.get(expected_date)
            if row is None:
                if earliest is not None and expected_date >= earliest:
                    return history, SignalStatus.MISSING_FEATURE
                continue
            if row.feature_status is not FeatureStatus.OK or row.value is None:
                if row.feature_status is FeatureStatus.PIT_UNSAFE:
                    return history, SignalStatus.PIT_UNSAFE
                return history, SignalStatus.FEATURE_ERROR
            history.append(row)
        return history, None

    def _result(
        self,
        definition: SignalDefinition,
        target: FeatureResult,
        history: list[FeatureResult],
        *,
        forced_status: SignalStatus | None,
        as_of: date | None,
    ) -> NormalizedSignalResult:
        involved = [*history, target]
        flags = _propagated_flags(involved)
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
        }
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
        )
        return self._remember_revision(result)

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
            history, forced_status = self._history_for_target(
                definition, target, by_date, frequency=frequency
            )
            results.append(
                self._result(
                    definition,
                    target,
                    history,
                    forced_status=forced_status,
                    as_of=date_as_of,
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
