"""Hard request, storage, and wall-clock budget accounting."""

import time
from collections.abc import Callable
from decimal import Decimal

from app.validation.schemas import BudgetLimits, BudgetSnapshot, StopReason

CheckpointCallback = Callable[[StopReason], None]


class BudgetStop(RuntimeError):
    """Raised before an operation would cross a hard Stage 3F limit."""

    def __init__(self, reason: StopReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class BudgetTracker:
    """Track logical work separately from every external HTTP attempt."""

    def __init__(
        self,
        limits: BudgetLimits,
        *,
        checkpoint: CheckpointCallback,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._checkpoint = checkpoint
        self._monotonic = monotonic
        self._started = monotonic()
        self.logical_pit_units = 0
        self.completed_logical_units = 0
        self.external_alfred_http_requests = 0
        self.retry_count = 0
        self.cache_reuse_count = 0
        self.incremental_storage_bytes = 0

    def _stop(self, reason: StopReason) -> None:
        self._checkpoint(reason)
        raise BudgetStop(reason)

    def check_time(self) -> None:
        """Stop after checkpointing when the wall-clock limit is reached."""
        if self.elapsed_seconds >= self.limits.wall_clock_seconds:
            self._stop(StopReason.WALL_CLOCK_BUDGET)

    def before_http_attempt(self, *, attempt_number: int) -> None:
        """Count retries and prevent request 2001 before network execution."""
        self.check_time()
        if self.external_alfred_http_requests >= self.limits.external_alfred_http_requests:
            self._stop(StopReason.REQUEST_BUDGET)
        self.external_alfred_http_requests += 1
        if attempt_number > 1:
            self.retry_count += 1

    def begin_logical_unit(self) -> None:
        """Count a planned PIT unit independently of provider attempts."""
        self.check_time()
        self.logical_pit_units += 1

    def complete_logical_unit(self, *, reused: bool = False) -> None:
        """Mark one logical unit complete and optionally count local/cache reuse."""
        self.completed_logical_units += 1
        if reused:
            self.cache_reuse_count += 1

    def update_storage(self, incremental_bytes: int) -> None:
        """Fail closed as soon as incremental Stage 3F storage reaches its cap."""
        self.incremental_storage_bytes = max(0, incremental_bytes)
        if self.incremental_storage_bytes >= self.limits.incremental_storage_bytes:
            self._stop(StopReason.STORAGE_BUDGET)

    @property
    def elapsed_seconds(self) -> float:
        """Return monotonic elapsed time."""
        return max(0.0, self._monotonic() - self._started)

    def snapshot(self) -> BudgetSnapshot:
        """Return typed counters suitable for a checkpoint."""
        return BudgetSnapshot(
            logical_pit_units=self.logical_pit_units,
            completed_logical_units=self.completed_logical_units,
            external_alfred_http_requests=self.external_alfred_http_requests,
            retry_count=self.retry_count,
            cache_reuse_count=self.cache_reuse_count,
            incremental_storage_bytes=self.incremental_storage_bytes,
            elapsed_wall_seconds=Decimal(str(round(self.elapsed_seconds, 6))),
        )
