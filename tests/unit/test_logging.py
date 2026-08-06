"""Structured-log redaction tests."""

from app.logging import redact_secrets


def test_log_processor_redacts_nested_sensitive_values() -> None:
    event = {
        "event": "failure",
        "api_key": "secret",
        "nested": {"password": "hidden", "safe": "visible"},
    }
    result = redact_secrets(None, "error", event)
    assert result["api_key"] == "***"
    assert result["nested"] == {"password": "***", "safe": "visible"}
