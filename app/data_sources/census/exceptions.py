"""Safe Census API exception hierarchy."""


class CensusError(RuntimeError):
    """Base error safe for logs and request audit."""


class CensusRequestError(CensusError):
    """HTTP or transport failure after bounded retries."""

    def __init__(self, message: str, *, status_code: int | None, attempt_count: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempt_count = attempt_count


class CensusBusinessError(CensusError):
    """HTTP success whose body reports a Census business error."""


class CensusResponseError(CensusError):
    """Malformed, incomplete, or semantically invalid Census response."""


class CensusIdentityError(CensusResponseError):
    """Returned dimensions do not exactly identify the configured series."""
