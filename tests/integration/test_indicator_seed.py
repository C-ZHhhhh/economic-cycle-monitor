"""Indicator registry seed integration tests."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import IndicatorRegistry
from app.indicators.config import load_indicator_catalog
from app.indicators.service import seed_indicator_catalog

pytestmark = pytest.mark.integration


def test_indicator_catalog_seed_is_idempotent(postgres_session: Session) -> None:
    catalog = load_indicator_catalog()

    first = seed_indicator_catalog(postgres_session, catalog)
    second = seed_indicator_catalog(postgres_session, catalog)

    assert first.inserted + first.updated + first.unchanged == 33
    assert second.unchanged == 33
    assert postgres_session.scalar(select(func.count()).select_from(IndicatorRegistry)) == 33
