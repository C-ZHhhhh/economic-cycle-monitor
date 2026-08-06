"""Normalize BLS periods, missing values, and footnotes."""

from datetime import date

from app.data_sources.bls.exceptions import BlsResponseError
from app.data_sources.bls.schemas import BlsSeries
from app.data_sources.official.schemas import OfficialObservation


class BlsAdapter:
    """Convert BLS source records into provider-neutral observations."""

    @staticmethod
    def observations(series: BlsSeries) -> list[OfficialObservation]:
        """Parse monthly periods and filter annual-average records by default."""
        observations: list[OfficialObservation] = []
        for point in series.data:
            if point.period in {"M13", "A01"}:
                continue
            if not point.period.startswith("M") or len(point.period) != 3:
                raise BlsResponseError(f"Unsupported BLS period {point.period!r}")
            try:
                month = int(point.period[1:])
                observation_date = date(point.year, month, 1)
            except ValueError as error:
                raise BlsResponseError(f"Invalid BLS monthly period {point.period!r}") from error
            footnotes = [item.model_dump() for item in point.footnotes]
            preliminary = any(item.code == "P" for item in point.footnotes)
            observations.append(
                OfficialObservation(
                    date=observation_date,
                    value=point.value,
                    source_period=point.period,
                    is_preliminary=preliminary,
                    footnotes=footnotes,
                    details={"period_name": point.periodName, "latest": point.latest},
                )
            )
        return sorted(observations, key=lambda item: item.date)
