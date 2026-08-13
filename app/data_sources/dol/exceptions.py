"""Safe DOL adapter exceptions."""


class DolError(RuntimeError):
    """Base class for expected DOL source failures."""


class DolRequestError(DolError):
    """Transport or HTTP failure."""

    def __init__(self, message: str, *, status_code: int | None, attempt_count: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempt_count = attempt_count


class DolResponseError(DolError):
    """Malformed or partial Weekly Claims response."""


class DolIdentityError(DolError):
    """Response dimensions do not match the configured mapping identity."""


class DolOperationalDegradation(DolError):
    """Temporary official source issue that must not invalidate a mapping."""
