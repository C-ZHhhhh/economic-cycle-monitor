"""Safe system configuration routes."""

from typing import Any, cast

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/config-summary")
def config_summary(request: Request) -> dict[str, Any]:
    """Return allow-listed settings with every credential redacted."""
    return cast(dict[str, Any], request.app.state.settings.safe_summary())
