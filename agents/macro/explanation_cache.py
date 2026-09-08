"""Small bounded process-local caches for explanation artifacts."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from agents.macro.explanation_schemas import MacroExplanationNarrative, MacroExplanationSnapshot


class BoundedLRU[ValueT: (MacroExplanationSnapshot, MacroExplanationNarrative)]:
    """Minimal deterministic LRU with no persistence or external dependencies."""

    def __init__(self, max_entries: int = 64) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._items: OrderedDict[object, ValueT] = OrderedDict()

    def get(self, key: object) -> ValueT | None:
        """Return a defensive copy and refresh recency."""
        value = self._items.get(key)
        if value is None:
            return None
        self._items.move_to_end(key)
        return value.model_copy(deep=True)

    def put(self, key: object, value: ValueT) -> None:
        """Store a defensive copy and evict the least-recently-used item."""
        self._items[key] = value.model_copy(deep=True)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


@dataclass(frozen=True)
class NarrativeCacheKey:
    """Complete non-secret identity of one LLM presentation artifact."""

    snapshot_calculation_hash: str
    prompt_hash: str
    output_schema_version: str
    provider: str
    model: str | None
    generation_settings_hash: str
