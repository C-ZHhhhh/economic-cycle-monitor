"""Bounded process-local cache for Stage 3E regime results."""

from collections import OrderedDict
from dataclasses import dataclass

from app.regimes.schemas import RegimeResult


@dataclass(frozen=True)
class RegimeCacheKey:
    """All frozen identities capable of changing one regime result."""

    regime_spec_version: str
    regime_spec_hash: str
    evaluation_context: str
    stage3d_spec_version: str
    stage3d_spec_hash: str
    current_macro_state_hash: str
    trajectory_lag_date: str
    lagged_macro_state_hash: str


class RegimeResultCache:
    """Small copy-safe LRU with strict current/PIT separation."""

    def __init__(self, max_entries: int = 128) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._items: OrderedDict[RegimeCacheKey, RegimeResult] = OrderedDict()

    def get(self, key: RegimeCacheKey) -> RegimeResult | None:
        """Return and refresh one cached result."""
        value = self._items.get(key)
        if value is None:
            return None
        self._items.move_to_end(key)
        return value.model_copy(deep=True)

    def put(self, key: RegimeCacheKey, value: RegimeResult) -> None:
        """Store a result and evict the least-recently-used item."""
        self._items[key] = value.model_copy(deep=True)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        """Discard all cached results."""
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
