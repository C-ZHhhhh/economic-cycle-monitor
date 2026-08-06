"""Canonical macro-result contract tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agents.macro.schemas import MacroCycleResult


def test_macro_result_accepts_stage_one_mock() -> None:
    result = MacroCycleResult(run_id=uuid4(), as_of=datetime.now(UTC))
    payload = result.model_dump(mode="json")
    assert payload["agent"] == "us_macro_cycle_agent"
    assert payload["country"] == "US"
    assert payload["current_regime"] is None
    assert payload["data_quality_status"] == "not_run"
    assert payload["regime_probabilities"] == {}


def test_macro_result_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        MacroCycleResult(run_id=uuid4(), as_of=datetime.now())


def test_macro_result_rejects_invalid_probability_distribution() -> None:
    with pytest.raises(ValidationError, match="must sum to 1"):
        MacroCycleResult(
            run_id=uuid4(),
            as_of=datetime.now(UTC),
            regime_probabilities={"expansion": 0.6, "contraction": 0.3},
        )
