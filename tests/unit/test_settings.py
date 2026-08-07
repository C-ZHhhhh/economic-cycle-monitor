"""Settings validation and redaction tests."""

import pytest
from pydantic import ValidationError

from app.settings import Settings


def test_settings_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "cycle-test")
    monkeypatch.setenv("FETCH_CONCURRENCY", "3")
    settings = Settings(_env_file=None)
    assert settings.app_name == "cycle-test"
    assert settings.fetch_concurrency == 3


def test_safe_summary_redacts_every_key(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = {
        "DEEPSEEK_API_KEY": "deepseek-secret-value",
        "FRED_API_KEY": "fred-secret-value",
        "BEA_API_KEY": "bea-secret-value",
        "BLS_API_KEY": "bls-secret-value",
        "CENSUS_API_KEY": "census-secret-value",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    summary = Settings(_env_file=None).safe_summary()
    rendered = str(summary)
    assert all(value not in rendered for value in secrets.values())
    assert summary["deepseek_api_key"] == "***"
    assert summary["bls_api_key"] == "***"
    assert "database_url" not in summary


def test_live_llm_requires_model_and_key() -> None:
    with pytest.raises(ValidationError, match="Live LLM mode requires"):
        Settings(_env_file=None, llm_mode="live", llm_model=None, deepseek_api_key=None)


def test_stage_one_rejects_parallel_analysis() -> None:
    with pytest.raises(ValidationError, match="concurrency must remain 1"):
        Settings(_env_file=None, analysis_concurrency=2)
