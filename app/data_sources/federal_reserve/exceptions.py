"""Safe Federal Reserve Board adapter exceptions."""


class FederalReserveError(RuntimeError):
    """Base class for expected Federal Reserve source failures."""


class FederalReserveRequestError(FederalReserveError):
    """Transport or HTTP failure with credential-free metadata."""

    def __init__(self, message: str, *, status_code: int | None, attempt_count: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempt_count = attempt_count


class FederalReserveResponseError(FederalReserveError):
    """Malformed, partial, or unexpected official payload."""


class FederalReserveIdentityError(FederalReserveError):
    """Official payload does not match the configured series identity."""


class FederalReserveBaseYearError(FederalReserveIdentityError):
    """G.17 index base changed and must be reviewed before writing."""
