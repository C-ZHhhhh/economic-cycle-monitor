"""Bounded process-local cache for Stage 3C pillar results."""

from collections import OrderedDict
from dataclasses import dataclass

from app.pillars.schemas import PillarResult


@dataclass(frozen=True)
class PillarCacheKey:
    """All identities capable of changing one pillar calculation."""

    pillar_id: str
    pillar_spec_version: str
    pillar_spec_hash: str
    evaluation_context: str
    normalization_spec_hash: str
    registry_hash: str
    constituent_calculation_hashes: tuple[str, ...]
    supporting_input_hashes: tuple[str, ...]


class PillarResultCache:
    """Small copy-safe LRU with no cross-process or provider state."""

    def __init__(self, max_entries: int = 128) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._items: OrderedDict[PillarCacheKey, PillarResult] = OrderedDict()

    def get(self, key: PillarCacheKey) -> PillarResult | None:
        """Return and refresh one cached pillar result."""
        value = self._items.get(key)
        if value is None:
            return None
        self._items.move_to_end(key)
        return value.model_copy(deep=True)

    def put(self, key: PillarCacheKey, value: PillarResult) -> None:
        """Store a result and evict least-recently-used entries."""
        self._items[key] = value.model_copy(deep=True)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        """Discard all cached pillar results."""
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
