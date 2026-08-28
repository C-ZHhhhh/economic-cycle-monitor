"""Bounded process-local cache for Stage 3D macro-state results."""

from collections import OrderedDict
from dataclasses import dataclass

from app.composites.schemas import MacroStateResult


@dataclass(frozen=True)
class MacroStateCacheKey:
    """All identities capable of changing one Stage 3D state."""

    state_id: str
    composite_spec_version: str
    composite_spec_hash: str
    evaluation_context: str
    pillar_spec_version: str
    pillar_spec_hash: str
    ordered_pillar_calculation_hashes: tuple[str, ...]


class MacroStateResultCache:
    """Small copy-safe LRU with strict current/PIT separation."""

    def __init__(self, max_entries: int = 128) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._items: OrderedDict[MacroStateCacheKey, MacroStateResult] = OrderedDict()

    def get(self, key: MacroStateCacheKey) -> MacroStateResult | None:
        """Return and refresh one cached state."""
        value = self._items.get(key)
        if value is None:
            return None
        self._items.move_to_end(key)
        return value.model_copy(deep=True)

    def put(self, key: MacroStateCacheKey, value: MacroStateResult) -> None:
        """Store a state and evict the least-recently-used item."""
        self._items[key] = value.model_copy(deep=True)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        """Discard all cached states."""
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
