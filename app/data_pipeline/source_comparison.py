"""Paged, auditable comparison of independently stored official and fallback data."""

from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_pipeline.repository import finish_sync_run, start_sync_run
from app.data_pipeline.schemas import SourceComparisonOptions, SyncSummary
from app.data_quality.checks import QualityIssue, Severity, official_source_issue
from app.data_quality.repository import persist_quality_issues, reconcile_quality_issues
from app.database.models import (
    IndicatorRegistry,
    IndicatorSourceMapping,
    RawObservation,
    SourceComparisonResult,
)
from app.indicators.repository import get_indicator
from app.source_mappings.config import load_source_mapping_catalog_for
from app.source_mappings.policy import source_mapping_is_direct
from app.source_mappings.repository import list_source_mappings, seed_source_mappings
from app.source_mappings.schemas import SourceMappingDefinition

_VALUE_CONFLICT = "source_value_conflict"


class SourceComparisonService:
    """Compare one supported official source and FRED in bounded pages."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.definitions: dict[str, SourceMappingDefinition] = {}

    def compare(self, options: SourceComparisonOptions) -> SyncSummary:
        """Compare selected mappings without resolving conflicts automatically."""
        catalog = load_source_mapping_catalog_for(options.primary_source)
        self.definitions = {item.indicator_id: item for item in catalog.mappings}
        seed_source_mappings(self.session, catalog)
        mappings = self._select_mappings(options)
        run = start_sync_run(
            self.session,
            "compare_sources",
            options.model_dump(mode="json"),
            len(mappings),
        )
        summary = SyncSummary(
            run_id=run.run_id,
            operation=run.operation,
            status="succeeded",
            indicator_count=len(mappings),
        )
        for mapping in mappings:
            indicator = get_indicator(self.session, mapping.indicator_id)
            if indicator is None:
                summary.skipped_count += 1
                continue
            counters: Counter[str] = Counter()
            issues: list[QualityIssue] = []
            latest_primary_date = self.session.scalar(
                select(func.max(RawObservation.observation_date)).where(
                    RawObservation.indicator_id == indicator.indicator_id,
                    RawObservation.source == options.primary_source,
                )
            )
            offset = 0
            while True:
                dates = self._date_page(indicator.indicator_id, options, offset)
                if not dates:
                    break
                rows = self.session.scalars(
                    select(RawObservation).where(
                        RawObservation.indicator_id == indicator.indicator_id,
                        RawObservation.source.in_(
                            [options.primary_source, options.secondary_source]
                        ),
                        RawObservation.observation_date.in_(dates),
                    )
                ).all()
                values = {(row.source, row.observation_date): row for row in rows}
                for observation_date in dates:
                    primary = values.get((options.primary_source, observation_date))
                    secondary = values.get((options.secondary_source, observation_date))
                    result, issue = self._compare_point(
                        run_id=run.run_id,
                        indicator=indicator,
                        mapping=mapping,
                        observation_date=observation_date,
                        primary=primary,
                        secondary=secondary,
                        options=options,
                        latest_primary_date=latest_primary_date,
                    )
                    counters[result.comparison_status] += 1
                    if not options.dry_run:
                        self.session.add(result)
                    if issue is not None:
                        issues.append(issue)
                self.session.flush()
                offset += len(dates)
            if options.dry_run or options.start is not None or options.end is not None:
                summary.quality_event_count += persist_quality_issues(
                    self.session, issues, run_id=run.run_id
                )
            else:
                # A complete, persisted comparison is the authority for value
                # conflicts: one that no longer reproduces (both sources now carry the
                # same revised history) is closed instead of blocking the indicator
                # forever. Conflicts that still reproduce stay open and fail closed.
                conflicts = [item for item in issues if item.event_type == _VALUE_CONFLICT]
                inserted, _resolved = reconcile_quality_issues(
                    self.session,
                    conflicts,
                    run_id=run.run_id,
                    indicator_id=indicator.indicator_id,
                    event_types=[_VALUE_CONFLICT],
                )
                summary.quality_event_count += inserted + persist_quality_issues(
                    self.session,
                    [item for item in issues if item.event_type != _VALUE_CONFLICT],
                    run_id=run.run_id,
                )
            summary.success_count += 1
            summary.results.append(
                {"indicator_id": indicator.indicator_id, "counts": dict(sorted(counters.items()))}
            )
        finish_sync_run(self.session, run, summary)
        return summary

    def _select_mappings(self, options: SourceComparisonOptions) -> list[IndicatorSourceMapping]:
        if (
            options.primary_source not in {"bls", "bea", "census", "federal_reserve", "dol"}
            or options.secondary_source != "fred"
        ):
            return []
        mappings = list_source_mappings(
            self.session,
            source=options.primary_source,
            indicator_id=options.indicator_id,
            enabled=True if options.enabled_only else None,
            limit=100,
        )
        selected = [
            item
            for item in mappings
            if item.mapping_status in {"valid", "valid_with_warning"}
            and (definition := self.definitions.get(item.indicator_id)) is not None
            and source_mapping_is_direct(definition.source_parameters)
        ]
        if options.module is None:
            return selected
        return [
            item
            for item in selected
            if (indicator := get_indicator(self.session, item.indicator_id)) is not None
            and indicator.module == options.module
        ]

    def _date_page(
        self, indicator_id: str, options: SourceComparisonOptions, offset: int
    ) -> list[date]:
        statement = (
            select(RawObservation.observation_date)
            .where(
                RawObservation.indicator_id == indicator_id,
                RawObservation.source.in_([options.primary_source, options.secondary_source]),
            )
            .distinct()
        )
        if options.start is not None:
            statement = statement.where(RawObservation.observation_date >= options.start)
        if options.end is not None:
            statement = statement.where(RawObservation.observation_date <= options.end)
        return list(
            self.session.scalars(
                statement.order_by(RawObservation.observation_date)
                .offset(offset)
                .limit(options.page_size)
            ).all()
        )

    def _compare_point(
        self,
        *,
        run_id: UUID,
        indicator: IndicatorRegistry,
        mapping: IndicatorSourceMapping,
        observation_date: date,
        primary: RawObservation | None,
        secondary: RawObservation | None,
        options: SourceComparisonOptions,
        latest_primary_date: date | None,
    ) -> tuple[SourceComparisonResult, QualityIssue | None]:
        definition = self.definitions[indicator.indicator_id]
        primary_value = Decimal(primary.value) if primary is not None else None
        secondary_value = Decimal(secondary.value) if secondary is not None else None
        status: str
        issue: QualityIssue | None = None
        difference: Decimal | None = None
        relative: Decimal | None = None
        if definition.expected_unit != indicator.unit:
            status = "normalization_mismatch"
            issue = official_source_issue(
                indicator.indicator_id,
                event_type=status,
                severity="error",
                source=options.primary_source,
                message="Source comparison blocked by incompatible units",
                details={
                    "primary_unit": definition.expected_unit,
                    "secondary_unit": indicator.unit,
                },
            )
        elif definition.expected_seasonal_adjustment != indicator.seasonal_adjustment:
            status = "seasonal_adjustment_mismatch"
            issue = official_source_issue(
                indicator.indicator_id,
                event_type=status,
                severity="error",
                source=options.primary_source,
                message="Source comparison blocked by incompatible seasonal adjustment",
            )
        elif (
            options.primary_source == "census"
            and primary is not None
            and not self._census_identity_is_auditable(primary, definition)
        ):
            status = "normalization_mismatch"
            issue = official_source_issue(
                indicator.indicator_id,
                event_type=status,
                severity="error",
                source=options.primary_source,
                message="Census comparison lacks exact identity, scale, or precision evidence",
            )
        elif (
            options.primary_source in {"federal_reserve", "dol"}
            and primary is not None
            and not self._public_release_identity_is_auditable(
                primary, definition, options.primary_source
            )
        ):
            status = "normalization_mismatch"
            issue = official_source_issue(
                indicator.indicator_id,
                event_type=status,
                severity="error",
                source=options.primary_source,
                message="Source comparison lacks exact identity, period, unit, or base evidence",
            )
        elif mapping.normalization_rule not in {
            "identity",
            "bea_unit_multiplier",
            "billions_to_millions",
            "identity_with_validated_base",
        }:
            status = "not_comparable"
            issue = official_source_issue(
                indicator.indicator_id,
                event_type="normalization_mismatch",
                severity="error",
                source=options.primary_source,
                message="Configured normalization rule is not implemented",
                details={"normalization_rule": mapping.normalization_rule},
            )
        elif (
            mapping.normalization_rule == "bea_unit_multiplier"
            and primary is not None
            and not self._bea_normalization_is_auditable(primary)
        ):
            status = "normalization_mismatch"
            issue = official_source_issue(
                indicator.indicator_id,
                event_type=status,
                severity="error",
                source=options.primary_source,
                message="BEA canonical value lacks consistent UNIT_MULT audit evidence",
            )
        elif primary_value is None and secondary_value is None:
            status = "not_comparable"
        elif primary_value is None:
            status = f"missing_in_{options.primary_source}"
            issue = official_source_issue(
                indicator.indicator_id,
                event_type="missing_in_primary_source",
                severity="warning",
                source=options.primary_source,
                message=(
                    "Observation is missing from the primary "
                    f"{options.primary_source.upper()} source"
                ),
                details={"observation_date": observation_date},
            )
        elif secondary_value is None:
            retrieved_age_days = (
                (date.today() - primary.retrieved_at.date()).days if primary is not None else 0
            )
            severity: Severity
            if (
                observation_date == latest_primary_date
                and retrieved_age_days <= mapping.distribution_lag_days
            ):
                status = "distribution_lag"
                severity = "info"
                event_type = "distribution_lag"
                message = (
                    "Fallback distribution has not yet published the "
                    f"{options.primary_source.upper()} observation"
                )
            else:
                status = "missing_in_fred"
                severity = "warning"
                event_type = "missing_in_fallback_source"
                message = "Observation is missing from the FRED fallback source"
            issue = official_source_issue(
                indicator.indicator_id,
                event_type=event_type,
                severity=severity,
                source=options.secondary_source,
                message=message,
                details={
                    "observation_date": observation_date,
                    "primary_retrieved_age_days": retrieved_age_days,
                    "distribution_lag_days": mapping.distribution_lag_days,
                },
            )
        else:
            difference = abs(primary_value - secondary_value)
            denominator = abs(primary_value)
            relative = difference / denominator if denominator else None
            if difference == 0:
                status = "match"
            elif difference <= Decimal(mapping.comparison_tolerance):
                status = "match_with_rounding"
            else:
                status = "source_value_conflict"
                issue = official_source_issue(
                    indicator.indicator_id,
                    event_type=status,
                    severity="error",
                    source=options.primary_source,
                    message="Aligned official and fallback values exceed configured tolerance",
                    details={
                        "observation_date": observation_date,
                        "absolute_difference": str(difference),
                        "tolerance": str(mapping.comparison_tolerance),
                    },
                )
        result = SourceComparisonResult(
            run_id=run_id,
            indicator_id=indicator.indicator_id,
            observation_date=observation_date,
            primary_source=options.primary_source,
            secondary_source=options.secondary_source,
            primary_value=primary_value,
            secondary_value=secondary_value,
            normalized_primary_value=primary_value,
            normalized_secondary_value=secondary_value,
            absolute_difference=difference,
            relative_difference=relative,
            tolerance=mapping.comparison_tolerance,
            comparison_status=status,
            details_json={
                "primary_retrieved_at": (
                    primary.retrieved_at.isoformat() if primary is not None else None
                ),
                "secondary_retrieved_at": (
                    secondary.retrieved_at.isoformat() if secondary is not None else None
                ),
                "normalization_rule": mapping.normalization_rule,
            },
        )
        return result, issue

    @staticmethod
    def _census_identity_is_auditable(
        observation: RawObservation, definition: SourceMappingDefinition
    ) -> bool:
        """Require exact Census dimensions and raw Decimal evidence before comparison."""
        details = observation.source_details_json
        identity = details.get("source_identity")
        parameters = definition.source_parameters
        if not isinstance(identity, dict):
            return False
        expected = {
            "dataset": parameters.get("dataset"),
            "program": parameters.get("program"),
            "category_code": parameters.get("category_code"),
            "data_type_code": parameters.get("data_type_code"),
            "seasonally_adj": parameters.get("seasonally_adj"),
            "geo_level_code": parameters.get("geo_level_code"),
            "geography": parameters.get("geography"),
        }
        if any(value is None or identity.get(key) != value for key, value in expected.items()):
            return False
        if details.get("scale") != parameters.get("scale"):
            return False
        if details.get("source_unit_code") != parameters.get("source_unit_code"):
            return False
        raw = details.get("raw_cell_value")
        if not isinstance(raw, str):
            return False
        try:
            return Decimal(raw.replace(",", "")) == Decimal(observation.value)
        except InvalidOperation:
            return False

    @staticmethod
    def _bea_normalization_is_auditable(observation: RawObservation) -> bool:
        """Require raw unit fields and a canonical value equal to the stored value."""
        details = observation.source_details_json
        required = {"raw_value", "cl_unit", "unit_mult", "canonical_value"}
        if not required.issubset(details) or details["canonical_value"] is None:
            return False
        try:
            return Decimal(str(details["canonical_value"])) == Decimal(observation.value)
        except (ArithmeticError, ValueError):
            return False

    @staticmethod
    def _public_release_identity_is_auditable(
        observation: RawObservation,
        definition: SourceMappingDefinition,
        source: str,
    ) -> bool:
        """Require exact release identity evidence before Board/DOL comparisons."""
        details = observation.source_details_json
        try:
            if Decimal(str(details.get("canonical_value"))) != Decimal(observation.value):
                return False
        except (ArithmeticError, ValueError):
            return False
        if source == "federal_reserve":
            parameters = definition.source_parameters
            if details.get("canonical_series_id") != definition.source_series_id:
                return False
            if details.get("frequency") != parameters.get("frequency"):
                return False
            if details.get("seasonal_adjustment") != parameters.get("seasonal_adjustment"):
                return False
            if parameters.get("release") == "G19":
                expected_wire = {
                    "raw_frequency": parameters.get("wire_frequency"),
                    "data_representation": parameters.get("data_representation"),
                    "wire_unit": parameters.get("wire_unit"),
                    "unit_multiplier": parameters.get("unit_multiplier"),
                    "canonical_unit": definition.source_unit,
                    "normalization_factor": "1",
                }
                if any(details.get(key) != value for key, value in expected_wire.items()):
                    return False
                try:
                    raw_value = Decimal(str(details.get("raw_value")))
                    canonical_value = Decimal(str(details.get("canonical_value")))
                except (ArithmeticError, ValueError):
                    return False
                if raw_value != canonical_value or canonical_value != Decimal(observation.value):
                    return False
            expected_base = parameters.get("expected_base_year")
            return expected_base is None or details.get("base_year") == expected_base
        return all(
            details.get(field)
            for field in ("scope", "program", "concept", "seasonal_adjustment", "frequency")
        )


def list_source_comparisons(
    session: Session,
    *,
    indicator_id: str | None = None,
    comparison_status: str | None = None,
    primary_source: str | None = None,
    secondary_source: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> list[SourceComparisonResult]:
    """Return a bounded newest-first comparison page."""
    statement = select(SourceComparisonResult)
    if indicator_id is not None:
        statement = statement.where(SourceComparisonResult.indicator_id == indicator_id)
    if comparison_status is not None:
        statement = statement.where(SourceComparisonResult.comparison_status == comparison_status)
    if primary_source is not None:
        statement = statement.where(SourceComparisonResult.primary_source == primary_source)
    if secondary_source is not None:
        statement = statement.where(SourceComparisonResult.secondary_source == secondary_source)
    return list(
        session.scalars(
            statement.order_by(SourceComparisonResult.created_at.desc()).offset(offset).limit(limit)
        ).all()
    )
