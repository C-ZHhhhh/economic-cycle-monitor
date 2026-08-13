"""Strict drift and completeness audit for the source registry."""

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.database.models import IndicatorRegistry
from app.indicators.schemas import IndicatorCatalog
from app.source_mappings.policy import source_mapping_is_direct, source_mapping_type
from app.source_mappings.schemas import SourceMappingCatalog, SourceMappingDefinition
from app.source_registry.config import storage_source_name
from app.source_registry.schemas import SourceRegistryCatalog, SourceSystem


class RegistryAuditIssue(BaseModel):
    """One deterministic registry validation finding."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning"]
    code: str
    message: str
    indicator_id: str | None = None


class RegistryAuditResult(BaseModel):
    """Serializable strict audit result."""

    model_config = ConfigDict(extra="forbid")

    registry_policy_version: str
    registry_hash: str
    indicator_count: int
    primary_distribution: dict[str, int]
    issues: list[RegistryAuditIssue] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return whether the audit contains no error findings."""
        return not any(item.severity == "error" for item in self.issues)


def _issue(code: str, message: str, indicator_id: str | None = None) -> RegistryAuditIssue:
    return RegistryAuditIssue(
        severity="error", code=code, message=message, indicator_id=indicator_id
    )


def _catalog_mappings(
    catalogs: Iterable[SourceMappingCatalog],
) -> dict[tuple[str, str], list[SourceMappingDefinition]]:
    result: dict[tuple[str, str], list[SourceMappingDefinition]] = {}
    for catalog in catalogs:
        for mapping in catalog.mappings:
            result.setdefault((mapping.indicator_id, mapping.source), []).append(mapping)
    return result


def audit_source_registry(
    registry: SourceRegistryCatalog,
    indicators: IndicatorCatalog,
    mapping_catalogs: Iterable[SourceMappingCatalog],
) -> RegistryAuditResult:
    """Validate inventory, authority, identities, semantics, and role boundaries."""
    issues: list[RegistryAuditIssue] = []
    policies = registry.by_indicator()
    definitions = {item.indicator_id: item for item in indicators.indicators}
    policy_ids = set(policies)
    indicator_ids = set(definitions)
    for missing in sorted(indicator_ids - policy_ids):
        issues.append(
            _issue("MISSING_REGISTRY_POLICY", "Indicator is absent from registry", missing)
        )
    for extra in sorted(policy_ids - indicator_ids):
        issues.append(_issue("EXTRA_REGISTRY_POLICY", "Registry policy has no indicator", extra))

    mappings = _catalog_mappings(mapping_catalogs)
    for indicator_id in sorted(policy_ids & indicator_ids):
        policy = policies[indicator_id]
        definition = definitions[indicator_id]
        if policy.canonical.frequency != definition.observation_frequency:
            issues.append(_issue("FREQUENCY_DRIFT", "Canonical frequency differs", indicator_id))
        if policy.canonical.unit != definition.expected_unit:
            issues.append(_issue("UNIT_DRIFT", "Canonical unit differs", indicator_id))
        if policy.canonical.seasonal_adjustment != definition.expected_seasonal_adjustment:
            issues.append(
                _issue("SEASONAL_DRIFT", "Canonical seasonal adjustment differs", indicator_id)
            )
        if policy.canonical.base_year != definition.canonical_base_year:
            issues.append(_issue("BASE_YEAR_DRIFT", "Canonical base year differs", indicator_id))

        if policy.primary_source is SourceSystem.FRED_CURRENT:
            if policy.primary_source_identity != definition.fred_series_id:
                issues.append(
                    _issue("SOURCE_IDENTITY_DRIFT", "FRED identity differs", indicator_id)
                )
        else:
            source = storage_source_name(policy.primary_source.value)
            candidates = mappings.get((indicator_id, source), [])
            direct = [
                item
                for item in candidates
                if item.enabled
                and source_mapping_is_direct(item.source_parameters)
                and item.mapping_status in {"valid", "valid_with_warning"}
            ]
            if len(direct) != 1:
                issues.append(
                    _issue(
                        "PRIMARY_MAPPING_CARDINALITY",
                        f"Expected one production-eligible DIRECT mapping, found {len(direct)}",
                        indicator_id,
                    )
                )
            elif direct[0].source_series_id != policy.primary_source_identity:
                issues.append(
                    _issue("SOURCE_IDENTITY_DRIFT", "Official identity differs", indicator_id)
                )

        for supporting in policy.supporting_sources:
            source = storage_source_name(supporting.source.value)
            matches = mappings.get((indicator_id, source), [])
            if not any(
                item.source_series_id == supporting.source_identity
                and source_mapping_type(item.source_parameters) == "SUPPORTING_COMPONENT"
                for item in matches
            ):
                issues.append(
                    _issue(
                        "SUPPORTING_COMPONENT_DRIFT",
                        "Configured supporting identity is absent or not SUPPORTING_COMPONENT",
                        indicator_id,
                    )
                )

        if policy.primary_source is SourceSystem.BLS:
            if not policy.history.verification_source.startswith(
                ("https://api.bls.gov/", "https://www.bls.gov/", "https://download.bls.gov/")
            ):
                issues.append(
                    _issue(
                        "BLS_BOUNDARY_EVIDENCE",
                        "BLS boundary lacks official evidence",
                        indicator_id,
                    )
                )
            if policy.comparison.mode != "EXACT" or policy.comparison.absolute_tolerance != 0:
                issues.append(
                    _issue("BLS_TOLERANCE_DRIFT", "BLS must use exact zero tolerance", indicator_id)
                )
        if policy.primary_source is SourceSystem.CENSUS and (
            policy.comparison.mode != "EXACT" or policy.comparison.absolute_tolerance != 0
        ):
            issues.append(
                _issue(
                    "CENSUS_TOLERANCE_DRIFT",
                    "Census must use exact zero tolerance",
                    indicator_id,
                )
            )
        if policy.primary_source is SourceSystem.DOL:
            candidates = mappings.get((indicator_id, "dol"), [])
            if not candidates or candidates[0].mapping_status != "valid":
                issues.append(
                    _issue("DOL_MAPPING_QUALITY_DRIFT", "DOL identity must be VALID", indicator_id)
                )

    retail = policies.get("US_REAL_RETAIL_SALES")
    if retail is not None and (
        retail.primary_source is not SourceSystem.FRED_CURRENT
        or retail.mapping_type != "DERIVED"
        or len(retail.supporting_sources) != 1
    ):
        issues.append(
            _issue(
                "RRSFS_ROLE_VIOLATION",
                "RRSFS must be FRED_CURRENT DERIVED with one supporting Census component",
                "US_REAL_RETAIL_SALES",
            )
        )

    return RegistryAuditResult(
        registry_policy_version=registry.registry_policy_version,
        registry_hash=registry.registry_hash,
        indicator_count=len(registry.policies),
        primary_distribution=dict(Counter(item.primary_source.value for item in registry.policies)),
        issues=issues,
    )


def audit_materialized_policy(
    registry: SourceRegistryCatalog,
    rows: Sequence[IndicatorRegistry],
) -> list[RegistryAuditIssue]:
    """Detect database drift from the YAML authority without modifying either side."""
    issues: list[RegistryAuditIssue] = []
    by_id = {item.indicator_id: item for item in rows}
    for policy in registry.policies:
        row = by_id.get(policy.indicator_id)
        if row is None:
            issues.append(
                _issue(
                    "MATERIALIZED_POLICY_MISSING",
                    "Indicator is absent from DB",
                    policy.indicator_id,
                )
            )
            continue
        expected_primary = storage_source_name(policy.primary_source.value)
        expected_fallback = (
            storage_source_name(policy.fallback_source.value)
            if policy.fallback_source is not None
            else None
        )
        if (
            row.primary_source != expected_primary
            or row.primary_source_series_id != policy.primary_source_identity
            or row.fallback_source != expected_fallback
        ):
            issues.append(
                _issue(
                    "MATERIALIZED_POLICY_DRIFT",
                    "Database primary/fallback tuple differs from source registry",
                    policy.indicator_id,
                )
            )
    return issues
