"""Normalize BEA NIPA values, units, periods, and notes."""

from datetime import date
from decimal import Decimal, InvalidOperation

from app.data_sources.bea.exceptions import BeaResponseError
from app.data_sources.bea.schemas import BeaNipaDataRow, BeaNote
from app.data_sources.official.schemas import OfficialObservation

MISSING_VALUES = {"", "--", "...", ".", "(NA)", "N/A", "NA"}


class BeaAdapter:
    """Convert exact BEA rows into canonical provider-neutral observations."""

    @staticmethod
    def observations(
        rows: list[BeaNipaDataRow],
        notes: list[BeaNote],
        *,
        frequency: str,
        target_unit_multiplier: int,
    ) -> list[OfficialObservation]:
        """Normalize rows without interpolation, resampling, or missing-to-zero coercion."""
        note_map = {note.NoteRef: note.NoteText for note in notes}
        observations: list[OfficialObservation] = []
        for row in rows:
            observation_date = BeaAdapter.parse_period(row.TimePeriod, frequency=frequency)
            raw_value = (row.DataValue or "").strip()
            value = BeaAdapter.parse_value(
                raw_value,
                unit_multiplier=row.UNIT_MULT,
                target_unit_multiplier=target_unit_multiplier,
            )
            refs = [item.strip() for item in (row.NoteRef or "").split(",") if item.strip()]
            observations.append(
                OfficialObservation(
                    date=observation_date,
                    value=value,
                    source_period=row.TimePeriod,
                    footnotes=[{"code": ref, "text": note_map.get(ref)} for ref in refs],
                    details={
                        "dataset_name": "NIPA",
                        "table_name": row.TableName,
                        "series_code": row.SeriesCode,
                        "line_number": row.LineNumber,
                        "line_description": row.LineDescription,
                        "metric_name": row.METRIC_NAME,
                        "cl_unit": row.CL_UNIT,
                        "unit_mult": row.UNIT_MULT,
                        "raw_value": row.DataValue,
                        "canonical_value": str(value) if value is not None else None,
                        "frequency": frequency.upper(),
                        "note_refs": refs,
                    },
                )
            )
        return sorted(observations, key=lambda item: item.date)

    @staticmethod
    def parse_period(value: str, *, frequency: str) -> date:
        """Parse a BEA annual, quarterly, or monthly period at its period start."""
        normalized = frequency.upper()
        try:
            if normalized == "Q" and len(value) == 6 and value[4] == "Q":
                quarter = int(value[5])
                if quarter not in {1, 2, 3, 4}:
                    raise ValueError
                return date(int(value[:4]), (quarter - 1) * 3 + 1, 1)
            if normalized == "M" and len(value) == 7 and value[4] == "M":
                return date(int(value[:4]), int(value[5:]), 1)
            if normalized == "A" and len(value) == 4:
                return date(int(value), 1, 1)
        except ValueError as error:
            raise BeaResponseError(f"Invalid BEA period {value!r}") from error
        raise BeaResponseError(
            f"BEA period {value!r} does not match requested frequency {normalized!r}"
        )

    @staticmethod
    def parse_value(
        value: str,
        *,
        unit_multiplier: str,
        target_unit_multiplier: int,
    ) -> Decimal | None:
        """Parse DataValue and convert its base-10 exponent to the canonical scale."""
        normalized = value.strip()
        if normalized.upper() in MISSING_VALUES:
            return None
        try:
            parsed = Decimal(normalized.replace(",", ""))
            source_multiplier = int(unit_multiplier)
        except (InvalidOperation, ValueError) as error:
            raise BeaResponseError("BEA DataValue or UNIT_MULT is invalid") from error
        return parsed * (Decimal(10) ** (source_multiplier - target_unit_multiplier))
