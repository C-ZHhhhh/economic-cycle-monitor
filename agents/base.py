"""Shared lifecycle contract for all future economic-cycle agents."""

from abc import ABC, abstractmethod
from collections.abc import Mapping

from pydantic import BaseModel


class BaseAgent[ResultT: BaseModel](ABC):
    """Minimal typed lifecycle that every agent must implement."""

    @abstractmethod
    async def validate_dependencies(self) -> None:
        """Raise when required dependencies are unavailable."""

    @abstractmethod
    async def collect_inputs(self) -> Mapping[str, object]:
        """Collect a point-in-time input snapshot."""

    @abstractmethod
    async def run_analysis(self, inputs: Mapping[str, object]) -> Mapping[str, object]:
        """Analyze collected inputs without formatting the final result."""

    @abstractmethod
    async def build_result(self, analysis: Mapping[str, object]) -> ResultT:
        """Build and validate the structured agent result."""

    @abstractmethod
    async def generate_report(self, result: ResultT) -> str | None:
        """Optionally render a human-readable report from structured output."""

    @abstractmethod
    async def persist_result(self, result: ResultT, report: str | None) -> None:
        """Persist the structured result and optional report."""
