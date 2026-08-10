"""Runtime construction for the BEA Data API client."""

from app.data_sources.bea.client import BeaClient
from app.data_sources.official.storage import OfficialFileCache, OfficialRawStore
from app.settings import Settings


def create_bea_client(settings: Settings) -> BeaClient:
    """Build a BEA client with secret-free cache and archive roots."""
    return BeaClient(
        api_key=settings.bea_api_key,
        base_url=str(settings.bea_base_url),
        timeout_seconds=settings.bea_timeout_seconds,
        max_retries=settings.bea_max_retries,
        retry_base_seconds=settings.bea_retry_base_seconds,
        cache=OfficialFileCache(
            settings.cache_dir / "bea",
            ttl_seconds=settings.bea_cache_ttl_seconds,
            enabled=settings.bea_cache_enabled,
        ),
        raw_store=OfficialRawStore(settings.raw_data_dir / "bea"),
    )
