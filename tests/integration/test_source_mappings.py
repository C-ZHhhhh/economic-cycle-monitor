"""PostgreSQL integration tests for source-neutral mappings."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import IndicatorSourceMapping
from app.indicators.config import load_indicator_catalog
from app.indicators.repository import get_indicator
from app.indicators.service import seed_indicator_catalog
from app.source_mappings.config import load_source_mapping_catalog
from app.source_mappings.repository import get_active_source_mapping, seed_source_mappings

pytestmark = pytest.mark.integration


def test_bls_mapping_seed_is_idempotent_and_updates_source_priority(
    postgres_session: Session,
) -> None:
    seed_indicator_catalog(postgres_session, load_indicator_catalog())
    catalog = load_source_mapping_catalog()

    first = seed_source_mappings(postgres_session, catalog)
    second = seed_source_mappings(postgres_session, catalog)

    assert first.inserted + first.unchanged == 14
    assert second.unchanged == 14
    assert postgres_session.scalar(select(func.count()).select_from(IndicatorSourceMapping)) == 14
    hires = get_indicator(postgres_session, "US_HIRES")
    assert hires is not None
    assert hires.primary_source == "bls"
    assert hires.primary_source_series_id == "JTS000000000000000HIR"
    assert hires.fallback_source == "fred"

    mapping = get_active_source_mapping(postgres_session, "US_HIRES", "bls")
    assert mapping is not None
    mapping.mapping_status = "valid_with_warning"
    mapping.mapping_warnings_json = ["runtime catalog warning"]
    mapping.metadata_checked_at = datetime.now(UTC)
    seed_source_mappings(postgres_session, catalog)
    assert mapping.mapping_status == "valid_with_warning"
    assert mapping.mapping_warnings_json == ["runtime catalog warning"]
