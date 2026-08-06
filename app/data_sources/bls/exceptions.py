"""Typed BLS transport and business-response failures."""


class BlsError(RuntimeError):
    """Base BLS adapter error."""


class BlsRequestError(BlsError):
    """HTTP, timeout, or retry exhaustion failure."""

    def __init__(
        self, message: str, *, status_code: int | None = None, attempt_count: int = 1
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempt_count = attempt_count


class BlsResponseError(BlsError):
    """Malformed successful HTTP response."""


class BlsBusinessError(BlsError):
    """HTTP-success response with a failed BLS business status."""

    def __init__(self, status: str) -> None:
        super().__init__(f"BLS returned business status {status!r}")
        self.status = status
