"""Stage 1 mock implementation of the US macro-cycle agent."""

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from agents.base import BaseAgent
from agents.macro.schemas import MacroCycleResult
from app.settings import Settings


class USMacroCycleAgent(BaseAgent[MacroCycleResult]):
    """Build a valid empty macro result without external calls or analysis."""

    def __init__(self, settings: Settings, *, run_id: UUID | None = None) -> None:
        self.settings = settings
        self.run_id = run_id or uuid4()
        self.persisted_result: MacroCycleResult | None = None

    async def validate_dependencies(self) -> None:
        """Validate that Stage 1 remains safely in mock mode."""
        if self.settings.llm_mode != "mock":
            raise RuntimeError("USMacroCycleAgent only supports mock mode in Stage 1")

    async def collect_inputs(self) -> Mapping[str, object]:
        """Return an empty snapshot because data collection is out of scope."""
        return {}

    async def run_analysis(self, inputs: Mapping[str, object]) -> Mapping[str, object]:
        """Return an empty analysis after asserting there are no real inputs."""
        if inputs:
            raise ValueError("Stage 1 mock analysis does not accept real inputs")
        return {}

    async def build_result(self, analysis: Mapping[str, object]) -> MacroCycleResult:
        """Build the formal empty result contract."""
        if analysis:
            raise ValueError("Stage 1 mock result does not accept analysis output")
        return MacroCycleResult(
            run_id=self.run_id,
            as_of=datetime.now(UTC),
            llm_provider=self.settings.llm_provider,
            llm_model=self.settings.llm_model,
        )

    async def generate_report(self, result: MacroCycleResult) -> str:
        """Render an explicitly labelled Stage 1 mock report."""
        return (
            "# US Macro Cycle Agent — Mock Report\n\n"
            f"Run ID: `{result.run_id}`\n\n"
            "No economic data, scoring, regime inference, or LLM call was performed.\n"
        )

    async def persist_result(self, result: MacroCycleResult, report: str | None) -> None:
        """Capture the result in memory; database persistence belongs to the service layer."""
        del report
        self.persisted_result = result
