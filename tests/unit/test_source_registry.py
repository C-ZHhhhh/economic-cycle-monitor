"""Offline contract tests for the unified source registry."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.indicators.config import load_indicator_catalog
from app.source_mappings.config import (
    SOURCE_MAPPING_PATHS,
    load_source_mapping_catalog_for,
)
from app.source_registry.audit import audit_source_registry
from app.source_registry.config import load_source_registry
from app.source_registry.health import expected_latest_period, mapping_quality_from_status
from app.source_registry.resolver import PITGranularityUnsupported, get_effective_observations
from app.source_registry.schemas import (
    MappingQuality,
    SourceRegistryCatalog,
    SourceRegistryPolicy,
    SourceSystem,
)
from app.source_registry.stitching import StitchingContract, validate_stitching_boundary


@pytest.fixture(scope="module")
def registry() -> SourceRegistryCatalog:
    """Load the tracked authority once for read-only unit tests."""
    return load_source_registry()


@pytest.mark.parametrize(
    "indicator_id",
    [item.indicator_id for item in load_indicator_catalog().indicators],
)
def test_each_inventory_policy_has_a_complete_contract(
    registry: SourceRegistryCatalog, indicator_id: str
) -> None:
    """Exercise every one of the 33 policies as an independent test case."""
    policy = registry.by_indicator()[indicator_id]
    assert policy.registry_policy_version == registry.registry_policy_version
    assert policy.primary_source_identity
    assert policy.canonical.frequency
    assert policy.date_semantics.observation_date
    assert policy.date_semantics.release_date
    assert policy.history.verification_source.startswith("https://")
    assert policy.history.verification_method
    assert policy.freshness.schedule_type
    assert policy.revision.recent_reconciliation.strategy
    assert policy.revision.annual_or_benchmark_reconciliation.strategy
    assert policy.revision.deep_reconciliation.strategy
    assert policy.vintage.granularity == "date"


def test_registry_and_indicator_inventory_are_exactly_equal(
    registry: SourceRegistryCatalog,
) -> None:
    catalog = load_indicator_catalog()
    assert len(registry.policies) == len(catalog.indicators) == 33
    assert set(registry.by_indicator()) == {item.indicator_id for item in catalog.indicators}


def test_primary_distribution_is_frozen(registry: SourceRegistryCatalog) -> None:
    counts = {
        source: sum(item.primary_source is source for item in registry.policies)
        for source in SourceSystem
    }
    assert counts == {
        SourceSystem.FRED_CURRENT: 2,
        SourceSystem.ALFRED_VINTAGE: 0,
        SourceSystem.BLS: 14,
        SourceSystem.BEA: 5,
        SourceSystem.CENSUS: 6,
        SourceSystem.FEDERAL_RESERVE: 4,
        SourceSystem.DOL: 2,
    }


def test_strict_config_audit_has_zero_drift(registry: SourceRegistryCatalog) -> None:
    result = audit_source_registry(
        registry,
        load_indicator_catalog(),
        [load_source_mapping_catalog_for(source) for source in SOURCE_MAPPING_PATHS],
    )
    assert result.passed
    assert result.issues == []


def test_missing_registry_policy_fails_audit(registry: SourceRegistryCatalog) -> None:
    incomplete = registry.model_copy(update={"policies": registry.policies[:-1]})
    result = audit_source_registry(
        incomplete,
        load_indicator_catalog(),
        [load_source_mapping_catalog_for(source) for source in SOURCE_MAPPING_PATHS],
    )
    assert not result.passed
    assert {item.code for item in result.issues} == {"MISSING_REGISTRY_POLICY"}


def test_duplicate_policy_is_rejected(registry: SourceRegistryCatalog) -> None:
    payload = registry.model_dump()
    payload["policies"] = [*payload["policies"], payload["policies"][0]]
    with pytest.raises(ValidationError, match="must be unique"):
        SourceRegistryCatalog.model_validate(payload)


def test_registry_hash_is_deterministic(registry: SourceRegistryCatalog) -> None:
    assert load_source_registry().registry_hash == registry.registry_hash
    assert len(registry.registry_hash) == 64


def test_registry_hash_changes_with_policy_version(
    registry: SourceRegistryCatalog, tmp_path: Path
) -> None:
    content = registry.source_path.read_text(encoding="utf-8")
    changed_path = tmp_path / "source_registry.yaml"
    changed_path.write_text(content.replace("stage2b5-v1", "stage2b5-v2"), encoding="utf-8")
    assert load_source_registry(changed_path).registry_hash != registry.registry_hash


@pytest.mark.parametrize(
    ("indicator_id", "series_id", "earliest"),
    [
        ("US_CPI", "CUSR0000SA0", date(1947, 1, 1)),
        ("US_CORE_CPI", "CUSR0000SA0L1E", date(1957, 1, 1)),
        ("US_PPI_COMMODITIES", "WPU00000000", date(1913, 1, 1)),
        ("US_NONFARM_EMPLOYMENT", "CES0000000001", date(1939, 1, 1)),
        ("US_UNEMPLOYMENT_RATE", "LNS14000000", date(1948, 1, 1)),
        ("US_U6_RATE", "LNS13327709", date(1994, 1, 1)),
        ("US_PARTICIPATION_RATE", "LNS11300000", date(1948, 1, 1)),
        ("US_EMPLOYMENT_POP_RATIO", "LNS12300000", date(1948, 1, 1)),
        ("US_JOB_OPENINGS", "JTS000000000000000JOL", date(2000, 12, 1)),
        ("US_HIRES", "JTS000000000000000HIR", date(2000, 12, 1)),
        ("US_QUITS", "JTS000000000000000QUR", date(2000, 12, 1)),
        ("US_TEMP_HELP_EMPLOYMENT", "CES6056132001", date(1990, 1, 1)),
        ("US_MANUFACTURING_HOURS", "CES3000000007", date(1939, 1, 1)),
        ("US_AVERAGE_HOURLY_EARNINGS", "CES0500000008", date(1964, 1, 1)),
    ],
)
def test_bls_earliest_primary_boundary_is_officially_versioned(
    registry: SourceRegistryCatalog,
    indicator_id: str,
    series_id: str,
    earliest: date,
) -> None:
    policy = registry.by_indicator()[indicator_id]
    assert policy.primary_source is SourceSystem.BLS
    assert policy.primary_source_identity == series_id
    assert policy.history.earliest_primary_date == earliest
    assert policy.history.verification_source.startswith("https://api.bls.gov/")
    assert "boundary probe" in policy.history.verification_method


def test_bls_and_census_comparisons_are_strict(registry: SourceRegistryCatalog) -> None:
    policies = [
        item
        for item in registry.policies
        if item.primary_source in {SourceSystem.BLS, SourceSystem.CENSUS}
    ]
    assert all(item.comparison.mode == "EXACT" for item in policies)
    assert all(item.comparison.absolute_tolerance == 0 for item in policies)
    assert all(not item.comparison.source_rounding_allowed for item in policies)


def test_bea_and_federal_reserve_rounding_evidence_is_retained(
    registry: SourceRegistryCatalog,
) -> None:
    policies = [
        item
        for item in registry.policies
        if item.primary_source in {SourceSystem.BEA, SourceSystem.FEDERAL_RESERVE}
    ]
    assert policies
    assert all(item.comparison.mode == "SOURCE_ROUNDING" for item in policies)
    assert all(item.comparison.source_rounding_allowed for item in policies)


def test_dol_mapping_quality_is_not_a_static_runtime_warning() -> None:
    catalog = load_source_mapping_catalog_for("dol")
    assert {item.mapping_status for item in catalog.mappings} == {"valid"}


def test_rrsfs_roles_are_fail_closed(registry: SourceRegistryCatalog) -> None:
    policy = registry.by_indicator()["US_REAL_RETAIL_SALES"]
    assert policy.primary_source is SourceSystem.FRED_CURRENT
    assert policy.mapping_type == "DERIVED"
    assert policy.supporting_sources[0].source is SourceSystem.CENSUS
    assert policy.supporting_sources[0].role == "SUPPORTING_COMPONENT"


def test_supporting_component_cannot_be_primary(registry: SourceRegistryCatalog) -> None:
    policy = registry.by_indicator()["US_REAL_RETAIL_SALES"]
    payload = policy.model_dump()
    payload["primary_source"] = "CENSUS"
    with pytest.raises(ValidationError, match="cannot also be primary"):
        SourceRegistryPolicy.model_validate(payload)


def test_alfred_cannot_be_current_primary(registry: SourceRegistryCatalog) -> None:
    payload = registry.policies[0].model_dump()
    payload["primary_source"] = "ALFRED_VINTAGE"
    with pytest.raises(ValidationError, match="cannot be a current primary"):
        SourceRegistryPolicy.model_validate(payload)


def test_alfred_cannot_be_current_fallback(registry: SourceRegistryCatalog) -> None:
    payload = registry.policies[0].model_dump()
    payload["fallback_source"] = "ALFRED_VINTAGE"
    with pytest.raises(ValidationError, match="cannot be a current fallback"):
        SourceRegistryPolicy.model_validate(payload)


def test_revision_families_are_not_flattened(registry: SourceRegistryCatalog) -> None:
    by_id = registry.by_indicator()
    assert (
        by_id["US_NONFARM_EMPLOYMENT"].revision.annual_or_benchmark_reconciliation.window_years == 5
    )
    assert by_id["US_JOB_OPENINGS"].revision.annual_or_benchmark_reconciliation.window_years == 5
    assert by_id["US_INITIAL_CLAIMS"].revision.annual_or_benchmark_reconciliation.window_years == 5
    assert (
        by_id["US_CONSUMER_CREDIT"].revision.recent_reconciliation.strategy
        == "full_official_release"
    )
    assert (
        by_id["US_REAL_GDP"].revision.deep_reconciliation.strategy
        == "comprehensive_update_full_history"
    )


def test_expected_period_uses_frequency_and_release_lag(
    registry: SourceRegistryCatalog,
) -> None:
    by_id = registry.by_indicator()
    assert expected_latest_period(by_id["US_CPI"], today=date(2026, 8, 13)) == date(2026, 7, 1)
    assert expected_latest_period(by_id["US_REAL_GDP"], today=date(2026, 8, 13)) == date(2026, 4, 1)
    assert expected_latest_period(by_id["US_INITIAL_CLAIMS"], today=date(2026, 8, 13)) == date(
        2026, 8, 8
    )


@pytest.mark.parametrize(
    ("status", "quality"),
    [
        ("valid", MappingQuality.VALID),
        ("valid_with_warning", MappingQuality.VALID_WITH_METADATA_WARNING),
        ("pending_review", MappingQuality.PENDING_REVIEW),
        ("invalid", MappingQuality.INVALID),
        ("unavailable", MappingQuality.UNAVAILABLE),
    ],
)
def test_mapping_quality_taxonomy_is_separate(status: str, quality: MappingQuality) -> None:
    assert mapping_quality_from_status(status) is quality


def test_timestamp_pit_is_rejected_before_database_access() -> None:
    with pytest.raises(PITGranularityUnsupported, match="PIT_GRANULARITY_UNSUPPORTED"):
        get_effective_observations(
            cast(Session, None),
            "US_REAL_GDP",
            date(2020, 1, 1),
            date(2020, 3, 31),
            datetime(2020, 6, 30, 12, 0),
        )


def test_only_gdp_has_validated_pit_date(registry: SourceRegistryCatalog) -> None:
    validated = [item for item in registry.policies if item.vintage.backtest_safety != "UNVERIFIED"]
    assert [item.indicator_id for item in validated] == ["US_REAL_GDP"]
    assert validated[0].vintage.validated_as_of_dates == [date(2020, 6, 30)]


def _stitch_contract() -> StitchingContract:
    return StitchingContract(
        frequency="monthly",
        unit="percent",
        scale=Decimal("1"),
        seasonal_adjustment="seasonally_adjusted",
        base_year=None,
    )


def test_stitching_is_blocked_without_explicit_policy(registry: SourceRegistryCatalog) -> None:
    policy = registry.by_indicator()["US_UNEMPLOYMENT_RATE"]
    result = validate_stitching_boundary(
        policy,
        primary_contract=_stitch_contract(),
        fallback_contract=_stitch_contract(),
        primary_values={date(1948, 1, 1): Decimal("4")},
        fallback_values={date(1948, 1, 1): Decimal("4")},
    )
    assert not result.allowed
    assert "POLICY_DOES_NOT_ALLOW_EARLY_HISTORY_FALLBACK" in result.reasons


def test_stitching_exact_overlap_can_pass_when_explicitly_allowed(
    registry: SourceRegistryCatalog,
) -> None:
    policy = registry.by_indicator()["US_UNEMPLOYMENT_RATE"]
    allowed = policy.model_copy(
        update={
            "history": policy.history.model_copy(
                update={"extension_policy": "FALLBACK_BEFORE_PRIMARY_START"}
            )
        }
    )
    result = validate_stitching_boundary(
        allowed,
        primary_contract=_stitch_contract(),
        fallback_contract=_stitch_contract(),
        primary_values={date(1948, 1, 1): Decimal("4")},
        fallback_values={date(1948, 1, 1): Decimal("4")},
    )
    assert result.allowed
    assert result.exact_count == 1


def test_stitching_rejects_semantic_mismatch(registry: SourceRegistryCatalog) -> None:
    policy = registry.by_indicator()["US_UNEMPLOYMENT_RATE"]
    allowed = policy.model_copy(
        update={
            "history": policy.history.model_copy(
                update={"extension_policy": "FALLBACK_BEFORE_PRIMARY_START"}
            )
        }
    )
    incompatible = _stitch_contract().model_copy(update={"unit": "ratio"})
    result = validate_stitching_boundary(
        allowed,
        primary_contract=_stitch_contract(),
        fallback_contract=incompatible,
        primary_values={date(1948, 1, 1): Decimal("4")},
        fallback_values={date(1948, 1, 1): Decimal("4")},
    )
    assert not result.allowed
    assert "CONTRACT_MISMATCH:unit" in result.reasons


def test_value_conflict_is_not_an_allowed_fallback_reason(
    registry: SourceRegistryCatalog,
) -> None:
    for policy in registry.policies:
        serialized = {item.value for item in policy.fallback.allowed_reasons}
        assert "VALUE_CONFLICT" not in serialized
        assert "NORMALIZATION_CONFLICT" not in serialized
        assert "SEASONAL_CONFLICT" not in serialized
