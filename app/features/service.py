"""Compute-on-demand Stage 3A features over canonical resolver observations."""

import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.features.cache import FeatureCacheKey, FeatureResultCache
from app.features.config import load_feature_catalog
from app.features.formulas import evaluate_formula, required_lookback, shift_period
from app.features.schemas import (
    FeatureCatalog,
    FeatureDefinition,
    FeatureResult,
    FeatureStatus,
    IndicatorFeatureSpec,
    QualityFlag,
)
from app.source_registry.config import load_source_registry
from app.source_registry.pit import get_effective_observations_on_demand
from app.source_registry.resolver import PITGranularityUnsupported
from app.source_registry.schemas import EffectiveObservation, RuntimeHealth, SourceRegistryCatalog

CanonicalResolver = Callable[..., Awaitable[list[EffectiveObservation]]]
_PROCESS_CACHE = FeatureResultCache(max_entries=256)


class FeatureEngineError(RuntimeError):
    """Base fail-closed Stage 3A computation error."""


class FeatureNotFound(FeatureEngineError):
    """A stable feature or indicator ID is absent from the specification."""


class FeatureRegistryMismatch(FeatureEngineError):
    """The feature authority is not bound to the active frozen source registry."""


def _observation_payload(item: EffectiveObservation) -> dict[str, Any]:
    return {
        "indicator_id": item.indicator_id,
        "observation_date": item.observation_date.isoformat(),
        "value": str(item.value),
        "unit": item.unit,
        "frequency": item.frequency,
        "source_used": item.source_used,
        "source_identity": item.source_identity,
        "source_policy_version": item.source_policy_version,
        "registry_hash": item.registry_hash,
        "mapping_version": item.mapping_version,
        "is_fallback": item.is_fallback,
        "provider_health": item.provider_health.value,
        "indicator_source_health": item.indicator_source_health.value,
        "release_date": item.release_date.isoformat() if item.release_date else None,
        "retrieved_at": item.retrieved_at.isoformat() if item.retrieved_at else None,
        "vintage_as_of": item.vintage_as_of.isoformat() if item.vintage_as_of else None,
        "vintage_source": item.vintage_source,
        "is_point_in_time_safe": item.is_point_in_time_safe,
    }


def _hash_payload(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _input_hash(observations: Sequence[EffectiveObservation]) -> str:
    return _hash_payload([_observation_payload(item) for item in observations])


def _quality_flags(inputs: Sequence[EffectiveObservation]) -> list[QualityFlag]:
    flags: set[QualityFlag] = set()
    if any(item.is_fallback for item in inputs):
        flags.add(QualityFlag.FALLBACK_INPUT)
    health = {item.indicator_source_health for item in inputs}
    if RuntimeHealth.STALE in health:
        flags.add(QualityFlag.STALE_INPUT)
    if RuntimeHealth.DEGRADED in health:
        flags.add(QualityFlag.DEGRADED_INPUT)
    if any(item.release_date is None for item in inputs):
        flags.add(QualityFlag.RELEASE_DATE_UNAVAILABLE)
    if len({(item.source_used, item.source_identity) for item in inputs}) > 1:
        flags.add(QualityFlag.MIXED_INPUT_SOURCES)
    return sorted(flags, key=str)


class FeatureEngine:
    """Compute bounded current or PIT features through one canonical data entry."""

    def __init__(
        self,
        session: Session,
        *,
        source_registry: SourceRegistryCatalog | None = None,
        feature_catalog: FeatureCatalog | None = None,
        cache: FeatureResultCache | None = None,
        resolver: CanonicalResolver = get_effective_observations_on_demand,
    ) -> None:
        self.session = session
        self.source_registry = source_registry or load_source_registry()
        self.feature_catalog = feature_catalog or load_feature_catalog(
            source_registry=self.source_registry
        )
        if (
            self.feature_catalog.required_source_registry_version
            != self.source_registry.registry_policy_version
            or self.feature_catalog.required_source_registry_hash
            != self.source_registry.registry_hash
        ):
            raise FeatureRegistryMismatch("REGISTRY_HASH_MISMATCH")
        self.cache = cache if cache is not None else FeatureResultCache()
        self.resolver = resolver
        self._revision_values: OrderedDict[tuple[str, date], tuple[date | None, Decimal | None]] = (
            OrderedDict()
        )

    async def _resolve(
        self,
        indicator: IndicatorFeatureSpec,
        features: Sequence[FeatureDefinition],
        *,
        start: date,
        end: date,
        as_of: date | datetime | None,
    ) -> list[EffectiveObservation]:
        policy = self.source_registry.by_indicator()[indicator.indicator_id]
        lookback = max(required_lookback(item.formula_id, item.window) for item in features)
        resolver_start = shift_period(start, policy.canonical.frequency, -lookback)
        return await self.resolver(
            self.session,
            indicator.indicator_id,
            resolver_start,
            end,
            as_of,
            registry=self.source_registry,
        )

    def _remember_revision(
        self,
        result: FeatureResult,
        *,
        max_entries: int = 512,
    ) -> FeatureResult:
        key = (result.feature_id, result.feature_observation_date)
        previous = self._revision_values.get(key)
        flags = list(result.quality_flags)
        if (
            previous is not None
            and previous[1] is not None
            and result.value is not None
            and previous[1] != result.value
        ):
            flags.append(QualityFlag.REVISION_CHANGED_RESULT)
        self._revision_values[key] = (result.as_of, result.value)
        self._revision_values.move_to_end(key)
        while len(self._revision_values) > max_entries:
            self._revision_values.popitem(last=False)
        return result.model_copy(update={"quality_flags": sorted(set(flags), key=str)})

    def _compute_series(
        self,
        indicator: IndicatorFeatureSpec,
        feature: FeatureDefinition,
        observations: Sequence[EffectiveObservation],
        *,
        start: date,
        end: date,
        as_of: date | None,
    ) -> list[FeatureResult]:
        policy = self.source_registry.by_indicator()[indicator.indicator_id]
        by_date = {item.observation_date: item for item in observations}
        results: list[FeatureResult] = []
        for target in sorted(item for item in by_date if start <= item <= end):
            outcome = evaluate_formula(
                feature.formula_id,
                window=feature.window,
                frequency=policy.canonical.frequency,
                target=target,
                observations=by_date,
            )
            status = outcome.status
            if (
                as_of is not None
                and outcome.inputs
                and not all(item.is_point_in_time_safe for item in outcome.inputs)
            ):
                status = FeatureStatus.PIT_UNSAFE
            inputs = list(outcome.inputs)
            payload = {
                "feature_specification_hash": self.feature_catalog.specification_hash,
                "feature_id": feature.feature_id,
                "formula_id": feature.formula_id.value,
                "window": feature.window,
                "target": target.isoformat(),
                "as_of": as_of.isoformat() if as_of else "CURRENT",
                "inputs": [_observation_payload(item) for item in inputs],
            }
            result = FeatureResult(
                feature_id=feature.feature_id,
                display_name=feature.display_name,
                description=feature.description,
                feature_spec_version=self.feature_catalog.feature_spec_version,
                formula_registry_version=self.feature_catalog.formula_registry_version,
                indicator_id=indicator.indicator_id,
                feature_observation_date=target,
                as_of=as_of,
                value=outcome.value if status is FeatureStatus.OK else None,
                unit=feature.output_unit,
                formula_id=feature.formula_id,
                window=feature.window,
                input_count=len(inputs),
                expected_input_count=len(outcome.expected_dates),
                input_observation_dates=[item.observation_date for item in inputs],
                input_sources=[item.source_used for item in inputs],
                input_source_identities=[item.source_identity for item in inputs],
                input_mapping_versions=[item.mapping_version for item in inputs],
                input_release_dates=[item.release_date for item in inputs],
                input_retrieved_at=[item.retrieved_at for item in inputs],
                input_vintage_dates=[item.vintage_as_of for item in inputs],
                source_policy_version=policy.registry_policy_version,
                registry_hash=self.source_registry.registry_hash,
                is_point_in_time_safe=(
                    as_of is not None
                    and bool(inputs)
                    and all(item.is_point_in_time_safe for item in inputs)
                ),
                feature_status=status,
                quality_flags=_quality_flags(inputs),
                calculation_hash=_hash_payload(payload),
            )
            results.append(self._remember_revision(result))
        return results

    async def _compute_indicator_selection(
        self,
        indicator: IndicatorFeatureSpec,
        features: Sequence[FeatureDefinition],
        *,
        start: date,
        end: date,
        as_of: date | datetime | None,
    ) -> list[FeatureResult]:
        if start > end:
            raise ValueError("start must not be after end")
        if isinstance(as_of, datetime):
            raise PITGranularityUnsupported("PIT_GRANULARITY_UNSUPPORTED")
        observations = await self._resolve(indicator, features, start=start, end=end, as_of=as_of)
        resolver_hash = _input_hash(observations)
        policy = self.source_registry.by_indicator()[indicator.indicator_id]
        date_as_of = as_of if isinstance(as_of, date) and not isinstance(as_of, datetime) else None
        results: list[FeatureResult] = []
        for feature in features:
            key = FeatureCacheKey(
                feature_id=feature.feature_id,
                feature_spec_version=self.feature_catalog.feature_spec_version,
                formula_registry_version=self.feature_catalog.formula_registry_version,
                indicator_id=indicator.indicator_id,
                start=start,
                end=end,
                as_of=date_as_of,
                source_policy_version=policy.registry_policy_version,
                registry_hash=self.source_registry.registry_hash,
                resolver_input_hash=resolver_hash,
            )
            cached = self.cache.get(key)
            if cached is not None:
                results.extend(cached)
                continue
            computed = self._compute_series(
                indicator,
                feature,
                observations,
                start=start,
                end=end,
                as_of=date_as_of,
            )
            self.cache.put(key, computed)
            results.extend(computed)
        return results

    async def compute_feature(
        self,
        feature_id: str,
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> list[FeatureResult]:
        """Compute one stable feature over an inclusive native-period range."""
        try:
            indicator, feature = self.feature_catalog.by_feature()[feature_id]
        except KeyError as error:
            raise FeatureNotFound(f"UNKNOWN_FEATURE:{feature_id}") from error
        return await self._compute_indicator_selection(
            indicator, [feature], start=start, end=end, as_of=as_of
        )

    async def compute_indicator_features(
        self,
        indicator_id: str,
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> list[FeatureResult]:
        """Compute every approved feature using one resolver call for the indicator."""
        try:
            indicator = self.feature_catalog.by_indicator()[indicator_id]
        except KeyError as error:
            raise FeatureNotFound(f"UNKNOWN_INDICATOR:{indicator_id}") from error
        return await self._compute_indicator_selection(
            indicator, indicator.features, start=start, end=end, as_of=as_of
        )

    async def compute_feature_set(
        self,
        feature_ids: Sequence[str],
        start: date,
        end: date,
        as_of: date | datetime | None = None,
    ) -> list[FeatureResult]:
        """Compute a selected set, grouping resolver work by canonical indicator."""
        by_feature = self.feature_catalog.by_feature()
        grouped: OrderedDict[str, tuple[IndicatorFeatureSpec, list[FeatureDefinition]]] = (
            OrderedDict()
        )
        for feature_id in feature_ids:
            try:
                indicator, feature = by_feature[feature_id]
            except KeyError as error:
                raise FeatureNotFound(f"UNKNOWN_FEATURE:{feature_id}") from error
            grouped.setdefault(indicator.indicator_id, (indicator, []))[1].append(feature)
        results: list[FeatureResult] = []
        for indicator, features in grouped.values():
            results.extend(
                await self._compute_indicator_selection(
                    indicator, features, start=start, end=end, as_of=as_of
                )
            )
        return results


async def compute_feature(
    session: Session,
    feature_id: str,
    start: date,
    end: date,
    as_of: date | datetime | None = None,
) -> list[FeatureResult]:
    """Public domain API for one configured feature."""
    return await FeatureEngine(session, cache=_PROCESS_CACHE).compute_feature(
        feature_id, start, end, as_of
    )


async def compute_indicator_features(
    session: Session,
    indicator_id: str,
    start: date,
    end: date,
    as_of: date | datetime | None = None,
) -> list[FeatureResult]:
    """Public domain API for all configured features of one indicator."""
    return await FeatureEngine(session, cache=_PROCESS_CACHE).compute_indicator_features(
        indicator_id, start, end, as_of
    )


async def compute_feature_set(
    session: Session,
    feature_ids: Sequence[str],
    start: date,
    end: date,
    as_of: date | datetime | None = None,
) -> list[FeatureResult]:
    """Public domain API for a selected configured feature set."""
    return await FeatureEngine(session, cache=_PROCESS_CACHE).compute_feature_set(
        feature_ids, start, end, as_of
    )
