"""Abstract interface for future economic data sources."""

from abc import ABC, abstractmethod
from datetime import date

from app.data_sources.schemas import SourceObservation


class BaseDataSource(ABC):
    """Fetch normalized observations without prescribing a provider."""

    @abstractmethod
    async def fetch(
        self,
        indicator_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[SourceObservation]:
        """Fetch observations for one configured indicator."""
