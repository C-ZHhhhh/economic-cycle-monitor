"""Runtime construction for the Census EITS client."""

from app.data_sources.census.client import CensusClient
from app.data_sources.official.storage import OfficialFileCache, OfficialRawStore
from app.settings import Settings


def create_census_client(settings: Settings) -> CensusClient:
    """Build a Census client with secret-free cache and archive roots."""
    return CensusClient(
        api_key=settings.census_api_key,
        base_url=str(settings.census_base_url),
        timeout_seconds=settings.census_timeout_seconds,
        max_retries=settings.census_max_retries,
        retry_base_seconds=settings.census_retry_base_seconds,
        cache=OfficialFileCache(
            settings.cache_dir / "census",
            ttl_seconds=settings.census_cache_ttl_seconds,
            enabled=settings.census_cache_enabled,
        ),
        raw_store=OfficialRawStore(settings.raw_data_dir / "census"),
    )
