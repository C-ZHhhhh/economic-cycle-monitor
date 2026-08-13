"""Factory for the DOL National Weekly Claims client."""

from app.data_sources.dol.client import DolClient
from app.data_sources.official.storage import OfficialFileCache, OfficialRawStore
from app.settings import Settings


def create_dol_client(settings: Settings) -> DolClient:
    """Build a secret-free client for bounded official XML requests."""
    return DolClient(
        base_url=str(settings.dol_base_url),
        timeout_seconds=settings.dol_timeout_seconds,
        max_retries=settings.dol_max_retries,
        retry_base_seconds=settings.dol_retry_base_seconds,
        cache=OfficialFileCache(
            settings.cache_dir / "dol",
            ttl_seconds=settings.dol_cache_ttl_seconds,
            enabled=settings.dol_cache_enabled,
            suffix="xml.gz",
        ),
        raw_store=OfficialRawStore(settings.raw_data_dir / "dol", suffix="xml.gz"),
    )
