"""Typed BEA transport, response, and business failures."""


class BeaError(RuntimeError):
    """Base BEA adapter error."""


class BeaRequestError(BeaError):
    """HTTP, timeout, or retry exhaustion failure."""

    def __init__(
        self, message: str, *, status_code: int | None = None, attempt_count: int = 1
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempt_count = attempt_count


class BeaResponseError(BeaError):
    """Malformed HTTP-success response."""


class BeaBusinessError(BeaError):
    """HTTP-success response containing a BEA API business error."""

    def __init__(self, code: str | None, description: str) -> None:
        self.code = code
        self.description = description
        code_text = f" {code}" if code else ""
        super().__init__(f"BEA returned business error{code_text}: {description}")
