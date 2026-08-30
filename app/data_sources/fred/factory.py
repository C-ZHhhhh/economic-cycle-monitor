"""Runtime construction for the FRED client."""

from app.data_sources.fred.cache import FredFileCache
from app.data_sources.fred.client import AttemptGuard, FredClient
from app.data_sources.fred.raw_store import FredRawStore
from app.settings import Settings


def create_fred_client(
    settings: Settings, *, attempt_guard: AttemptGuard | None = None
) -> FredClient:
    """Build a FRED client without exposing its credential to callers."""
    return FredClient(
        api_key=settings.fred_api_key,
        base_url=str(settings.fred_base_url),
        timeout_seconds=settings.fred_timeout_seconds,
        max_retries=settings.fred_max_retries,
        retry_base_seconds=settings.fred_retry_base_seconds,
        cache=FredFileCache(
            settings.cache_dir / "fred",
            ttl_seconds=settings.fred_cache_ttl_seconds,
            enabled=settings.fred_cache_enabled,
        ),
        raw_store=FredRawStore(settings.raw_data_dir / "fred"),
        attempt_guard=attempt_guard,
    )
