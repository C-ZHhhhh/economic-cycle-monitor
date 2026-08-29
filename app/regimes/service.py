"""Classify Stage 3D MacroStateResult objects without lower-layer access."""

import hashlib
import json
from collections.abc import Awaitable
from datetime import date
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from app.composites.config import load_composite_catalog
from app.composites.schemas import (
    CompositeCatalog,
    EvaluationMode,
    MacroStateResult,
    MacroStateStatus,
)
from app.composites.service import MacroStateEngine
from app.regimes.cache import RegimeCacheKey, RegimeResultCache
from app.regimes.calculations import (
    classification_margin,
    classify_growth,
    classify_growth_trajectory,
    classify_inflation,
    classify_inflation_trajectory,
    subtract_calendar_months,
)
from app.regimes.config import load_regime_catalog
from app.regimes.schemas import (
    UPSTREAM_HISTORICAL_FLAG_MAP,
    AxisTrajectoryResult,
    RegimeCatalog,
    RegimeQualityFlag,
    RegimeResult,
    RegimeStatus,
    TrajectoryStatus,
)

_PROCESS_CACHE = RegimeResultCache(max_entries=128)


class MacroStateService(Protocol):
    """The only business boundary accepted by Stage 3E."""

    def compute_macro_state(
        self, evaluation_as_of: date | None = None
    ) -> Awaitable[MacroStateResult]:
        """Return one current or date-EOD Stage 3D state."""
        ...


class MacroRegimeEngineError(RuntimeError):
    """Base Stage 3E domain error."""


class RegimeContractMismatch(MacroRegimeEngineError):
    """Stage 3E and Stage 3D authorities do not match."""


def _hash_payload(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class MacroRegimeEngine:
    """Create one mechanical level regime plus independent trajectory qualifiers."""

    def __init__(
        self,
        session: Session,
        *,
        composite_catalog: CompositeCatalog | None = None,
        regime_catalog: RegimeCatalog | None = None,
        macro_state_engine: MacroStateService | None = None,
        cache: RegimeResultCache | None = None,
    ) -> None:
        self.session = session
        self.composite_catalog = composite_catalog or load_composite_catalog()
        self.regime_catalog = regime_catalog or load_regime_catalog(
            composite_catalog=self.composite_catalog
        )
        catalog = self.regime_catalog
        composite = self.composite_catalog
        if (
            catalog.required_stage3d_spec_version != composite.composite_spec_version
            or catalog.required_stage3d_spec_hash != composite.specification_hash
            or catalog.required_source_registry_version
            != composite.required_source_registry_version
            or catalog.required_source_registry_hash != composite.required_source_registry_hash
            or catalog.required_feature_spec_version != composite.required_feature_spec_version
            or catalog.required_normalization_spec_version
            != composite.required_normalization_spec_version
            or catalog.required_normalization_spec_hash
            != composite.required_normalization_spec_hash
            or catalog.required_pillar_spec_version != composite.required_pillar_spec_version
            or catalog.required_pillar_spec_hash != composite.required_pillar_spec_hash
        ):
            raise RegimeContractMismatch("REGIME_CONTRACT_MISMATCH")
        self.macro_state_engine = macro_state_engine or MacroStateEngine(
            session, composite_catalog=composite
        )
        self.cache = cache if cache is not None else RegimeResultCache()

    def _cache_key(
        self,
        current: MacroStateResult,
        lagged: MacroStateResult,
        evaluation_as_of: date | None,
        lag_date: date,
    ) -> RegimeCacheKey:
        return RegimeCacheKey(
            regime_spec_version=self.regime_catalog.regime_spec_version,
            regime_spec_hash=self.regime_catalog.specification_hash,
            evaluation_context=evaluation_as_of.isoformat() if evaluation_as_of else "CURRENT",
            stage3d_spec_version=self.composite_catalog.composite_spec_version,
            stage3d_spec_hash=self.composite_catalog.specification_hash,
            current_macro_state_hash=current.calculation_hash,
            trajectory_lag_date=lag_date.isoformat(),
            lagged_macro_state_hash=lagged.calculation_hash,
        )

    def _contract_failure(
        self,
        state: MacroStateResult,
        *,
        expected_date: date,
        expected_mode: EvaluationMode,
    ) -> RegimeStatus | None:
        catalog = self.regime_catalog
        if (
            state.state_id != self.composite_catalog.state_id
            or state.composite_spec_version != catalog.required_stage3d_spec_version
            or state.composite_spec_hash != catalog.required_stage3d_spec_hash
            or state.source_registry_hash != catalog.required_source_registry_hash
            or state.feature_spec_version != catalog.required_feature_spec_version
            or state.normalization_spec_version != catalog.required_normalization_spec_version
            or state.normalization_spec_hash != catalog.required_normalization_spec_hash
            or state.pillar_spec_version != catalog.required_pillar_spec_version
            or state.pillar_spec_hash != catalog.required_pillar_spec_hash
            or state.macro_state_status is MacroStateStatus.SPEC_MISMATCH
        ):
            return RegimeStatus.SPEC_MISMATCH
        if (
            state.evaluation_as_of != expected_date
            or state.evaluation_mode is not expected_mode
            or state.macro_state_status is MacroStateStatus.AS_OF_MISMATCH
        ):
            return RegimeStatus.AS_OF_MISMATCH
        if expected_mode is EvaluationMode.PIT_DATE_EOD and (
            not state.is_point_in_time_safe
            or state.macro_state_status is MacroStateStatus.PIT_UNSAFE
        ):
            return RegimeStatus.PIT_UNSAFE
        return None

    @staticmethod
    def _trajectory_failure_status(failure: RegimeStatus | None) -> TrajectoryStatus | None:
        if failure is RegimeStatus.SPEC_MISMATCH:
            return TrajectoryStatus.SPEC_MISMATCH
        if failure in {RegimeStatus.PIT_UNSAFE, RegimeStatus.AS_OF_MISMATCH}:
            return TrajectoryStatus.PIT_UNSAFE
        return None

    def _growth_trajectory(
        self,
        current: MacroStateResult,
        lagged: MacroStateResult,
        lag_date: date,
        *,
        current_failure: RegimeStatus | None,
        lag_failure: RegimeStatus | None,
    ) -> AxisTrajectoryResult:
        definition = self.regime_catalog.trajectory
        failure = self._trajectory_failure_status(
            current_failure
        ) or self._trajectory_failure_status(lag_failure)
        current_value = current.growth.value
        lag_value = lagged.growth.value
        if failure is not None:
            status = failure
        elif current_value is None or lag_value is None:
            status = TrajectoryStatus.INSUFFICIENT_HISTORY
        elif not self.regime_catalog.growth.minimum_confidence.accepts(
            current.growth.confidence
        ) or not self.regime_catalog.growth.minimum_confidence.accepts(lagged.growth.confidence):
            status = TrajectoryStatus.LOW_DATA_CONFIDENCE
        else:
            status = TrajectoryStatus.OK
        delta = (
            current_value - lag_value
            if status is TrajectoryStatus.OK and current_value is not None and lag_value is not None
            else None
        )
        qualifier = (
            classify_growth_trajectory(
                delta,
                lower=definition.lower_threshold,
                upper=definition.upper_threshold,
            )
            if delta is not None
            else None
        )
        return AxisTrajectoryResult(
            horizon_months=definition.horizon_months,
            current_as_of=current.evaluation_as_of,
            lag_as_of=lag_date,
            current_value=current_value,
            lag_value=lag_value,
            current_confidence=current.growth.confidence,
            lag_confidence=lagged.growth.confidence,
            delta=delta,
            qualifier=qualifier,
            status=status,
            current_state_hash=current.calculation_hash,
            lag_state_hash=lagged.calculation_hash,
        )

    def _inflation_trajectory(
        self,
        current: MacroStateResult,
        lagged: MacroStateResult,
        lag_date: date,
        *,
        current_failure: RegimeStatus | None,
        lag_failure: RegimeStatus | None,
    ) -> AxisTrajectoryResult:
        definition = self.regime_catalog.trajectory
        failure = self._trajectory_failure_status(
            current_failure
        ) or self._trajectory_failure_status(lag_failure)
        current_value = current.inflation.value
        lag_value = lagged.inflation.value
        if failure is not None:
            status = failure
        elif current_value is None or lag_value is None:
            status = TrajectoryStatus.INSUFFICIENT_HISTORY
        elif not self.regime_catalog.inflation.minimum_confidence.accepts(
            current.inflation.confidence
        ) or not self.regime_catalog.inflation.minimum_confidence.accepts(
            lagged.inflation.confidence
        ):
            status = TrajectoryStatus.LOW_DATA_CONFIDENCE
        else:
            status = TrajectoryStatus.OK
        delta = (
            current_value - lag_value
            if status is TrajectoryStatus.OK and current_value is not None and lag_value is not None
            else None
        )
        qualifier = (
            classify_inflation_trajectory(
                delta,
                lower=definition.lower_threshold,
                upper=definition.upper_threshold,
            )
            if delta is not None
            else None
        )
        return AxisTrajectoryResult(
            horizon_months=definition.horizon_months,
            current_as_of=current.evaluation_as_of,
            lag_as_of=lag_date,
            current_value=current_value,
            lag_value=lag_value,
            current_confidence=current.inflation.confidence,
            lag_confidence=lagged.inflation.confidence,
            delta=delta,
            qualifier=qualifier,
            status=status,
            current_state_hash=current.calculation_hash,
            lag_state_hash=lagged.calculation_hash,
        )

    def _build_result(
        self,
        current: MacroStateResult,
        lagged: MacroStateResult,
        evaluation_as_of: date | None,
        lag_date: date,
    ) -> RegimeResult:
        catalog = self.regime_catalog
        expected_mode = (
            EvaluationMode.PIT_DATE_EOD if evaluation_as_of is not None else EvaluationMode.CURRENT
        )
        expected_date = evaluation_as_of or date.today()
        current_failure = self._contract_failure(
            current, expected_date=expected_date, expected_mode=expected_mode
        )
        lag_failure = self._contract_failure(
            lagged,
            expected_date=lag_date,
            expected_mode=EvaluationMode.PIT_DATE_EOD,
        )

        growth_value = current.growth.value
        inflation_value = current.inflation.value
        growth_condition = (
            classify_growth(
                growth_value,
                lower=catalog.growth.lower_threshold,
                upper=catalog.growth.upper_threshold,
            )
            if growth_value is not None and current_failure is None
            else None
        )
        inflation_condition = (
            classify_inflation(
                inflation_value,
                lower=catalog.inflation.lower_threshold,
                upper=catalog.inflation.upper_threshold,
            )
            if inflation_value is not None and current_failure is None
            else None
        )
        growth_margin = (
            classification_margin(
                growth_value,
                lower=catalog.growth.lower_threshold,
                upper=catalog.growth.upper_threshold,
            )
            if growth_condition is not None and growth_value is not None
            else None
        )
        inflation_margin = (
            classification_margin(
                inflation_value,
                lower=catalog.inflation.lower_threshold,
                upper=catalog.inflation.upper_threshold,
            )
            if inflation_condition is not None and inflation_value is not None
            else None
        )

        flags = {
            mapped
            for upstream, mapped in UPSTREAM_HISTORICAL_FLAG_MAP.items()
            if upstream in current.quality_flags
        }
        if current.growth.confidence < Decimal(1) or current.inflation.confidence < Decimal(1):
            flags.add(RegimeQualityFlag.LOW_DATA_CONFIDENCE)
        margin_warning = catalog.classification_margin.warning_threshold
        if (
            growth_margin is not None
            and growth_margin <= margin_warning
            or inflation_margin is not None
            and inflation_margin <= margin_warning
        ):
            flags.add(RegimeQualityFlag.NEAR_REGIME_BOUNDARY)

        base = None
        if current_failure is not None:
            status = current_failure
        elif growth_value is None:
            status = RegimeStatus.INSUFFICIENT_STATE
        elif inflation_value is None:
            status = RegimeStatus.MISSING_INFLATION
        elif not catalog.growth.minimum_confidence.accepts(
            current.growth.confidence
        ) or not catalog.inflation.minimum_confidence.accepts(current.inflation.confidence):
            status = RegimeStatus.INSUFFICIENT_STATE
            flags.add(RegimeQualityFlag.LOW_DATA_CONFIDENCE)
        else:
            if growth_condition is None or inflation_condition is None:
                raise MacroRegimeEngineError("ELIGIBLE_AXES_REQUIRE_CONDITIONS")
            base = catalog.by_conditions()[(growth_condition, inflation_condition)]
            status = (
                RegimeStatus.OK
                if current.macro_state_status is MacroStateStatus.OK
                else RegimeStatus.PARTIAL
            )

        growth_trajectory = self._growth_trajectory(
            current,
            lagged,
            lag_date,
            current_failure=current_failure,
            lag_failure=lag_failure,
        )
        inflation_trajectory = self._inflation_trajectory(
            current,
            lagged,
            lag_date,
            current_failure=current_failure,
            lag_failure=lag_failure,
        )
        pit_safe = (
            expected_mode is EvaluationMode.PIT_DATE_EOD
            and current.is_point_in_time_safe
            and lagged.is_point_in_time_safe
            and current_failure is None
            and lag_failure is None
        )
        minimum_confidence = min(current.growth.confidence, current.inflation.confidence)
        result = RegimeResult(
            regime_spec_version=catalog.regime_spec_version,
            regime_spec_hash=catalog.specification_hash,
            evaluation_mode=current.evaluation_mode,
            evaluation_as_of=current.evaluation_as_of,
            macro_state_id=current.state_id,
            macro_state_calculation_hash=current.calculation_hash,
            stage3d_spec_version=current.composite_spec_version,
            stage3d_spec_hash=current.composite_spec_hash,
            growth_strength=growth_value,
            growth_data_confidence=current.growth.confidence,
            growth_status=current.growth.status,
            growth_condition=growth_condition,
            growth_classification_margin=growth_margin,
            inflation_pressure=inflation_value,
            inflation_data_confidence=current.inflation.confidence,
            inflation_status=current.inflation.status,
            inflation_condition=inflation_condition,
            inflation_classification_margin=inflation_margin,
            minimum_axis_data_confidence=minimum_confidence,
            base_regime_id=base.regime_id if base is not None else None,
            base_regime_display_name=base.display_name if base is not None else None,
            growth_trajectory=growth_trajectory,
            inflation_trajectory=inflation_trajectory,
            context=[item.model_copy(deep=True) for item in current.context],
            macro_state_status=current.macro_state_status,
            is_point_in_time_safe=pit_safe,
            regime_status=status,
            quality_flags=sorted(flags, key=str),
            source_registry_hash=current.source_registry_hash,
            feature_spec_version=current.feature_spec_version,
            normalization_spec_version=current.normalization_spec_version,
            normalization_spec_hash=current.normalization_spec_hash,
            pillar_spec_version=current.pillar_spec_version,
            pillar_spec_hash=current.pillar_spec_hash,
            calculation_hash="pending",
        )
        payload = result.model_dump(mode="json", exclude={"calculation_hash"})
        return result.model_copy(update={"calculation_hash": _hash_payload(payload)})

    async def compute_regime(self, evaluation_as_of: date | None = None) -> RegimeResult:
        """Compute one current or date-EOD regime through the Stage 3D boundary."""
        current = await self.macro_state_engine.compute_macro_state(evaluation_as_of)
        lag_date = subtract_calendar_months(
            current.evaluation_as_of, self.regime_catalog.trajectory.horizon_months
        )
        lagged = await self.macro_state_engine.compute_macro_state(lag_date)
        key = self._cache_key(current, lagged, evaluation_as_of, lag_date)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        result = self._build_result(current, lagged, evaluation_as_of, lag_date)
        self.cache.put(key, result)
        return result


async def compute_macro_regime(
    session: Session, evaluation_as_of: date | None = None
) -> RegimeResult:
    """Public Stage 3E domain API with no provider, weight, or formula inputs."""
    return await MacroRegimeEngine(session, cache=_PROCESS_CACHE).compute_regime(evaluation_as_of)
