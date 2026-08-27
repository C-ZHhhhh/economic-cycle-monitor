"""Bounded process-local cache for Stage 3B normalized signals."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date

from app.signals.schemas import NormalizedSignalResult


@dataclass(frozen=True)
class SignalCacheKey:
    """All identities capable of changing a computed signal series."""

    signal_id: str
    normalization_spec_version: str
    normalization_spec_hash: str
    feature_spec_version: str
    start: date
    end: date
    as_of: date | None
    registry_hash: str
    input_feature_calculation_hash: str
    history_feature_calculation_hash: str


class SignalResultCache:
    """Small copy-safe LRU; no cross-process state or external service."""

    def __init__(self, max_entries: int = 256) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._items: OrderedDict[SignalCacheKey, tuple[NormalizedSignalResult, ...]] = OrderedDict()

    def get(self, key: SignalCacheKey) -> list[NormalizedSignalResult] | None:
        """Return and refresh one cached signal series."""
        value = self._items.get(key)
        if value is None:
            return None
        self._items.move_to_end(key)
        return list(value)

    def put(self, key: SignalCacheKey, value: list[NormalizedSignalResult]) -> None:
        """Store one series and evict least-recently-used entries."""
        self._items[key] = tuple(value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        """Discard all computed signals."""
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
