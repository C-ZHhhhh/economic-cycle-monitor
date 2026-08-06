"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime settings with safe defaults for local development."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "economic-cycle-monitor"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: str = "postgresql+psycopg://macro:macro@localhost:5432/macro"
    test_database_url: str | None = None

    llm_provider: str = "deepseek"
    llm_base_url: HttpUrl = HttpUrl("https://api.deepseek.com")
    llm_model: str | None = None
    deepseek_api_key: SecretStr | None = None
    llm_mode: Literal["mock", "live"] = "mock"

    fred_api_key: SecretStr | None = None
    bea_api_key: SecretStr | None = None
    bls_api_key: SecretStr | None = None
    census_api_key: SecretStr | None = None

    fetch_concurrency: int = Field(default=4, ge=1, le=16)
    per_source_fetch_concurrency: int = Field(default=2, ge=1, le=8)
    analysis_concurrency: int = Field(default=1, ge=1)
    llm_concurrency: int = Field(default=1, ge=1)
    backtest_concurrency: int = Field(default=1, ge=1)
    scheduler_enabled: bool = False

    @model_validator(mode="after")
    def validate_stage_one_constraints(self) -> "Settings":
        """Enforce the resource and secret requirements for this stage."""
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must use postgresql+psycopg")
        if self.per_source_fetch_concurrency > self.fetch_concurrency:
            raise ValueError("PER_SOURCE_FETCH_CONCURRENCY cannot exceed FETCH_CONCURRENCY")
        if any(
            value != 1
            for value in (
                self.analysis_concurrency,
                self.llm_concurrency,
                self.backtest_concurrency,
            )
        ):
            raise ValueError("Stage 1 analysis, LLM, and backtest concurrency must remain 1")
        if self.scheduler_enabled:
            raise ValueError("SCHEDULER_ENABLED must remain false in Stage 1")
        if self.llm_mode == "live" and (not self.llm_model or self.deepseek_api_key is None):
            raise ValueError("Live LLM mode requires LLM_MODEL and DEEPSEEK_API_KEY")
        return self

    def safe_summary(self) -> dict[str, Any]:
        """Return a deliberately allow-listed configuration summary."""
        return {
            "app_env": self.app_env,
            "app_name": self.app_name,
            "log_level": self.log_level,
            "database_configured": bool(self.database_url),
            "llm_provider": self.llm_provider,
            "llm_base_url": str(self.llm_base_url),
            "llm_model": self.llm_model,
            "llm_mode": self.llm_mode,
            "deepseek_api_key": "***" if self.deepseek_api_key else None,
            "fred_api_key": "***" if self.fred_api_key else None,
            "bea_api_key": "***" if self.bea_api_key else None,
            "bls_api_key": "***" if self.bls_api_key else None,
            "census_api_key": "***" if self.census_api_key else None,
            "fetch_concurrency": self.fetch_concurrency,
            "per_source_fetch_concurrency": self.per_source_fetch_concurrency,
            "analysis_concurrency": self.analysis_concurrency,
            "llm_concurrency": self.llm_concurrency,
            "backtest_concurrency": self.backtest_concurrency,
            "scheduler_enabled": self.scheduler_enabled,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
