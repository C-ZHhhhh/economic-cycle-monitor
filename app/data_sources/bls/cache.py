"""Compatibility wrappers for the shared official-source cache."""

from app.data_sources.official.storage import OfficialFileCache, official_cache_key


def bls_cache_key(parameters: dict[str, object]) -> str:
    """Build a stable key while excluding every registration-key spelling."""
    return official_cache_key(
        parameters,
        secret_names={"registrationkey", "registration_key", "api_key"},
    )


class BlsFileCache(OfficialFileCache):
    """Backward-compatible BLS name for the shared safe cache."""
