"""Small process-local LRU for deterministic Stage 3A feature results."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date

from app.features.schemas import FeatureResult


@dataclass(frozen=True)
class FeatureCacheKey:
    """All identities capable of changing a computed feature set."""

    feature_id: str
    feature_spec_version: str
    formula_registry_version: str
    indicator_id: str
    start: date
    end: date
    as_of: date | None
    source_policy_version: str
    registry_hash: str
    resolver_input_hash: str


class FeatureResultCache:
    """Bounded LRU with copy-safe immutable Pydantic result values."""

    def __init__(self, max_entries: int = 256) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._items: OrderedDict[FeatureCacheKey, tuple[FeatureResult, ...]] = OrderedDict()

    def get(self, key: FeatureCacheKey) -> list[FeatureResult] | None:
        """Return and refresh one cached feature series."""
        value = self._items.get(key)
        if value is None:
            return None
        self._items.move_to_end(key)
        return list(value)

    def put(self, key: FeatureCacheKey, value: list[FeatureResult]) -> None:
        """Store one series and evict the least-recently-used entry."""
        self._items[key] = tuple(value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        """Discard all process-local computed values."""
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
