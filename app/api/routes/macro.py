"""US macro-agent contract routes."""

from typing import Any

from fastapi import APIRouter

from agents.macro.schemas import MacroCycleResult

router = APIRouter()


@router.get("/schema")
def macro_schema() -> dict[str, Any]:
    """Expose the formal JSON Schema consumed by future agents."""
    return MacroCycleResult.model_json_schema()
