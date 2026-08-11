"""Exact Census EITS row matching and precision-preserving normalization."""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data_sources.census.exceptions import CensusIdentityError, CensusResponseError
from app.data_sources.census.identity import (
    census_source_identity,
    census_source_identity_hash,
)
from app.data_sources.census.schemas import CensusMappingIdentity, CensusRow
from app.data_sources.official.schemas import OfficialObservation

MISSING_VALUES = {"", "NA", "N/A", "(NA)", "NULL", "--", "...", "."}


class CensusAdapter:
    """Convert exact multidimensional EITS rows into official observations."""

    @staticmethod
    def observations(
        rows: list[CensusRow],
        identity: CensusMappingIdentity,
        *,
        today: date | None = None,
    ) -> list[OfficialObservation]:
        """Match every identity dimension before parsing values or dates."""
        if identity.seasonally_adj is None or identity.geo_level_code is None:
            raise CensusIdentityError(
                "Census seasonal and geography wire values remain pending live confirmation"
            )
        category_rows = [row for row in rows if row.category_code == identity.category_code]
        if not category_rows:
            raise CensusIdentityError(f"unexpected_category: expected {identity.category_code}")
        data_type_rows = [
            row for row in category_rows if row.data_type_code == identity.data_type_code
        ]
        if not data_type_rows:
            returned = sorted({row.data_type_code for row in category_rows})
            raise CensusIdentityError(
                f"unexpected_data_type: expected {identity.data_type_code}, returned {returned}"
            )
        seasonal_rows = [
            row for row in data_type_rows if row.seasonally_adj == identity.seasonally_adj
        ]
        if not seasonal_rows:
            returned = sorted({row.seasonally_adj for row in data_type_rows})
            raise CensusIdentityError(
                f"unexpected_seasonal_adjustment: expected {identity.seasonally_adj}, "
                f"returned {returned}"
            )
        geography_rows = [
            row for row in seasonal_rows if row.geo_level_code == identity.geo_level_code
        ]
        if not geography_rows:
            returned = sorted({row.geo_level_code for row in seasonal_rows})
            raise CensusIdentityError(
                f"unexpected_geography: expected {identity.geo_level_code}, returned {returned}"
            )

        limit = today or date.today()
        current_month = date(limit.year, limit.month, 1)
        observations: list[OfficialObservation] = []
        for row in geography_rows:
            observation_date = CensusAdapter.parse_month(row)
            raw_value = row.cell_value
            value = CensusAdapter.parse_value(raw_value)
            if observation_date > current_month or value is None:
                continue
            source_identity = census_source_identity(identity, row)
            details: dict[str, Any] = {
                "dataset": identity.dataset,
                "program": identity.program,
                "category_code": row.category_code,
                "data_type_code": row.data_type_code,
                "seasonally_adj": row.seasonally_adj,
                "geo_level_code": row.geo_level_code,
                "geography": identity.geography,
                "source_unit_code": identity.source_unit_code,
                "scale": identity.scale,
                "raw_cell_value": raw_value,
                "canonical_value": str(value),
                "source_identity": source_identity,
                "source_identity_hash": census_source_identity_hash(source_identity),
                "returned_dimensions": row.returned_dimensions(),
                "error_data": row.error_data,
            }
            observations.append(
                OfficialObservation(
                    date=observation_date,
                    value=value,
                    source_period=row.time_slot_date or row.time,
                    details=details,
                )
            )
        return sorted(observations, key=lambda item: item.date)

    @staticmethod
    def parse_month(row: CensusRow) -> date:
        """Parse an EITS monthly slot without depending on response order."""
        candidates = [row.time_slot_date, row.time]
        for candidate in candidates:
            if candidate is None:
                continue
            normalized = candidate.strip()
            if len(normalized) >= 7 and normalized[4] == "-":
                try:
                    return date(int(normalized[:4]), int(normalized[5:7]), 1)
                except ValueError:
                    pass
        if len(row.time) == 4 and row.time_slot_id is not None:
            try:
                return date(int(row.time), int(row.time_slot_id), 1)
            except ValueError as error:
                raise CensusResponseError("Census monthly time slot is invalid") from error
        raise CensusResponseError("Census row does not contain a parseable monthly period")

    @staticmethod
    def parse_value(value: str | None) -> Decimal | None:
        """Parse cell_value directly as Decimal without a float round trip."""
        if value is None or value.strip().upper() in MISSING_VALUES:
            return None
        try:
            return Decimal(value.strip().replace(",", ""))
        except InvalidOperation as error:
            raise CensusResponseError("Census cell_value is not numeric") from error
