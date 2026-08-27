"""Typed internal failures for one ALFRED date-EOD availability request."""

from app.data_quality.scope import PITAvailabilityStatus, SafeErrorCategory


class PITAvailabilityFailure(RuntimeError):
    """A safely classified PIT request failure before audit persistence."""

    def __init__(
        self,
        status: PITAvailabilityStatus,
        *,
        safe_error_category: SafeErrorCategory,
        http_status: int | None = None,
        runtime_blocking: bool,
        request_recorded: bool = False,
    ) -> None:
        super().__init__(f"PIT_{status.value}")
        self.status = status
        self.safe_error_category = safe_error_category
        self.http_status = http_status
        self.runtime_blocking = runtime_blocking
        self.request_recorded = request_recorded
