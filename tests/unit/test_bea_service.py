"""Small offline tests for BEA request window planning."""

import pytest

from app.data_pipeline.bea_service import split_bea_years


def test_split_bea_years_uses_explicit_bounded_batches() -> None:
    assert split_bea_years(2000, 2011, 5) == [
        [2000, 2001, 2002, 2003, 2004],
        [2005, 2006, 2007, 2008, 2009],
        [2010, 2011],
    ]


def test_split_bea_years_rejects_reverse_range() -> None:
    with pytest.raises(ValueError, match="must not be after"):
        split_bea_years(2025, 2024, 5)
