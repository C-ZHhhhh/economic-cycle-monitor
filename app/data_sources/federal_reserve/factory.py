"""Factory for the Federal Reserve Board release client."""

from app.data_sources.federal_reserve.client import FederalReserveClient
from app.settings import Settings


def create_federal_reserve_client(settings: Settings) -> FederalReserveClient:
    """Build a client using only stable Board release endpoints."""
    return FederalReserveClient(
        base_url=str(settings.federal_reserve_base_url),
        timeout_seconds=settings.federal_reserve_timeout_seconds,
        max_retries=settings.federal_reserve_max_retries,
        retry_base_seconds=settings.federal_reserve_retry_base_seconds,
        cache_root=settings.cache_dir / "federal_reserve",
        raw_root=settings.raw_data_dir / "federal_reserve",
        cache_ttl_seconds=settings.federal_reserve_cache_ttl_seconds,
        cache_enabled=settings.federal_reserve_cache_enabled,
        zip_max_members=settings.federal_reserve_zip_max_members,
        zip_max_uncompressed_bytes=settings.federal_reserve_zip_max_uncompressed_bytes,
    )
