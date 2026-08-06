"""Typed FRED client failures."""


class FredError(RuntimeError):
    """Base class for FRED integration failures."""


class FredConfigurationError(FredError):
    """Raised when a FRED operation is requested without safe configuration."""


class FredRequestError(FredError):
    """Raised after a request fails or exhausts retry attempts."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        attempt_count: int = 1,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempt_count = attempt_count


class FredResponseError(FredError):
    """Raised when FRED returns a successful but structurally invalid response."""
