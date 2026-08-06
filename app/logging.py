"""Structured application logging with defensive secret redaction."""

import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, cast

import structlog

from app.settings import Settings

_SENSITIVE_FRAGMENTS = ("api_key", "password", "secret", "token", "database_url")


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "***"
            if any(part in str(key).lower() for part in _SENSITIVE_FRAGMENTS)
            else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def redact_secrets(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Redact values associated with sensitive-looking keys."""
    redacted = _redact_value(event_dict)
    return dict(redacted)


def configure_logging(settings: Settings) -> None:
    """Configure standard logging and structlog once for the process."""
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.log_level),
        stream=sys.stdout,
        force=True,
    )
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger() -> structlog.stdlib.BoundLogger:
    """Return a bound structured logger."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger())
