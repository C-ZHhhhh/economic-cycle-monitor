"""Application configuration loaded from environment variables."""

from datetime import date
from functools import lru_cache
from pathlib import Path
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
    fred_base_url: HttpUrl = HttpUrl("https://api.stlouisfed.org/fred")
    fred_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    fred_max_retries: int = Field(default=3, ge=0, le=8)
    fred_retry_base_seconds: float = Field(default=1.0, ge=0, le=30)
    fred_cache_enabled: bool = True
    fred_cache_ttl_seconds: int = Field(default=3600, ge=0)
    fred_default_backfill_start: date = date(2000, 1, 1)
    fred_weekly_lookback_days: int = Field(default=90, ge=1)
    fred_monthly_lookback_months: int = Field(default=18, ge=1)
    fred_quarterly_lookback_quarters: int = Field(default=12, ge=1)
    database_batch_size: int = Field(default=1000, ge=100, le=5000)
    raw_data_dir: Path = Path("data/raw")
    cache_dir: Path = Path("data/cache")
    output_dir: Path = Path("outputs")
    bea_enabled: bool = False
    bea_api_key: SecretStr | None = None
    bea_base_url: HttpUrl = HttpUrl("https://apps.bea.gov/api/data")
    bea_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    bea_max_retries: int = Field(default=3, ge=0, le=8)
    bea_retry_base_seconds: float = Field(default=1.0, ge=0, le=30)
    bea_cache_enabled: bool = True
    bea_cache_ttl_seconds: int = Field(default=3600, ge=0)
    bea_year_batch_size: int = Field(default=5, ge=1, le=20)
    bea_default_backfill_start_year: int = Field(default=2000, ge=1929, le=2100)
    bls_enabled: bool = False
    bls_api_key: SecretStr | None = None
    bls_base_url: HttpUrl = HttpUrl("https://api.bls.gov/publicAPI/v2")
    bls_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    bls_max_retries: int = Field(default=3, ge=0, le=8)
    bls_retry_base_seconds: float = Field(default=1.0, ge=0, le=30)
    bls_cache_enabled: bool = True
    bls_cache_ttl_seconds: int = Field(default=3600, ge=0)
    bls_registered_max_series: int = Field(default=50, ge=1, le=50)
    bls_registered_max_years: int = Field(default=20, ge=1, le=20)
    bls_unregistered_max_series: int = Field(default=25, ge=1, le=25)
    bls_unregistered_max_years: int = Field(default=10, ge=1, le=10)
    bls_default_backfill_start_year: int = Field(default=2000, ge=1913, le=2100)
    census_enabled: bool = False
    census_api_key: SecretStr | None = None
    census_base_url: HttpUrl = HttpUrl("https://api.census.gov/data")
    census_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    census_max_retries: int = Field(default=3, ge=0, le=8)
    census_retry_base_seconds: float = Field(default=1.0, ge=0, le=30)
    census_cache_enabled: bool = True
    census_cache_ttl_seconds: int = Field(default=3600, ge=0)
    census_year_batch_size: int = Field(default=5, ge=1, le=20)
    census_default_backfill_start_year: int = Field(default=2000, ge=1959, le=2100)
    federal_reserve_enabled: bool = False
    federal_reserve_base_url: HttpUrl = HttpUrl("https://www.federalreserve.gov")
    federal_reserve_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    federal_reserve_max_retries: int = Field(default=3, ge=0, le=8)
    federal_reserve_retry_base_seconds: float = Field(default=1.0, ge=0, le=30)
    federal_reserve_cache_enabled: bool = True
    federal_reserve_cache_ttl_seconds: int = Field(default=3600, ge=0)
    federal_reserve_zip_max_members: int = Field(default=128, ge=1, le=1000)
    federal_reserve_zip_max_uncompressed_bytes: int = Field(
        default=64 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024
    )
    dol_enabled: bool = False
    dol_base_url: HttpUrl = HttpUrl("https://oui.doleta.gov")
    dol_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    dol_max_retries: int = Field(default=3, ge=0, le=8)
    dol_retry_base_seconds: float = Field(default=1.0, ge=0, le=30)
    dol_cache_enabled: bool = True
    dol_cache_ttl_seconds: int = Field(default=3600, ge=0)
    dol_recent_observation_periods: int = Field(default=2, ge=2, le=12)
    dol_annual_reconciliation_years: int = Field(default=5, ge=1, le=10)

    fetch_concurrency: int = Field(default=4, ge=1, le=16)
    per_source_fetch_concurrency: int = Field(default=2, ge=1, le=8)
    analysis_concurrency: int = Field(default=1, ge=1)
    llm_concurrency: int = Field(default=1, ge=1)
    backtest_concurrency: int = Field(default=1, ge=1)
    scheduler_enabled: bool = False

    @model_validator(mode="after")
    def validate_stage_constraints(self) -> "Settings":
        """Enforce the resource and secret requirements for Stage 2."""
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
            raise ValueError("Stage 2 analysis, LLM, and backtest concurrency must remain 1")
        if self.scheduler_enabled:
            raise ValueError("SCHEDULER_ENABLED must remain false in Stage 2")
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
            "fred_base_url": str(self.fred_base_url),
            "fred_cache_enabled": self.fred_cache_enabled,
            "fred_cache_ttl_seconds": self.fred_cache_ttl_seconds,
            "bea_api_key": "***" if self.bea_api_key else None,
            "bea_enabled": self.bea_enabled,
            "bea_base_url": str(self.bea_base_url),
            "bea_cache_enabled": self.bea_cache_enabled,
            "bea_cache_ttl_seconds": self.bea_cache_ttl_seconds,
            "bls_enabled": self.bls_enabled,
            "bls_api_key": "***" if self.bls_api_key else None,
            "bls_base_url": str(self.bls_base_url),
            "bls_cache_enabled": self.bls_cache_enabled,
            "bls_cache_ttl_seconds": self.bls_cache_ttl_seconds,
            "census_api_key": "***" if self.census_api_key else None,
            "census_enabled": self.census_enabled,
            "census_base_url": str(self.census_base_url),
            "census_cache_enabled": self.census_cache_enabled,
            "census_cache_ttl_seconds": self.census_cache_ttl_seconds,
            "federal_reserve_enabled": self.federal_reserve_enabled,
            "federal_reserve_base_url": str(self.federal_reserve_base_url),
            "federal_reserve_cache_enabled": self.federal_reserve_cache_enabled,
            "federal_reserve_cache_ttl_seconds": self.federal_reserve_cache_ttl_seconds,
            "dol_enabled": self.dol_enabled,
            "dol_base_url": str(self.dol_base_url),
            "dol_cache_enabled": self.dol_cache_enabled,
            "dol_cache_ttl_seconds": self.dol_cache_ttl_seconds,
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
