"""Runtime construction for the BLS Public Data API client."""

from app.data_sources.bls.cache import BlsFileCache
from app.data_sources.bls.client import BlsClient
from app.data_sources.bls.raw_store import BlsRawStore
from app.settings import Settings


def create_bls_client(settings: Settings) -> BlsClient:
    """Build a BLS client using registered or public limits as appropriate."""
    is_registered = settings.bls_api_key is not None
    return BlsClient(
        api_key=settings.bls_api_key,
        base_url=str(settings.bls_base_url),
        timeout_seconds=settings.bls_timeout_seconds,
        max_retries=settings.bls_max_retries,
        retry_base_seconds=settings.bls_retry_base_seconds,
        max_series_per_request=(
            settings.bls_registered_max_series
            if is_registered
            else settings.bls_unregistered_max_series
        ),
        max_years_per_request=(
            settings.bls_registered_max_years
            if is_registered
            else settings.bls_unregistered_max_years
        ),
        cache=BlsFileCache(
            settings.cache_dir / "bls",
            ttl_seconds=settings.bls_cache_ttl_seconds,
            enabled=settings.bls_cache_enabled,
        ),
        raw_store=BlsRawStore(settings.raw_data_dir / "bls"),
    )
