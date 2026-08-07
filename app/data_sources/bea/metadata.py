"""Strict identity and semantic validation for candidate BEA NIPA mappings."""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data_sources.bea.adapter import BeaAdapter
from app.data_sources.bea.schemas import BeaNipaDataRow
from app.source_mappings.schemas import MappingStatus, SourceMappingDefinition


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _line_key(value: str) -> Decimal | None:
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return None


@dataclass(slots=True)
class BeaMetadataValidation:
    """Candidate validation evidence without automatically promoting mapping status."""

    configured_status: MappingStatus = "pending_review"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    returned_series_codes: list[str] = field(default_factory=list)
    returned_line_numbers: list[str] = field(default_factory=list)

    @property
    def status(self) -> MappingStatus:
        """Return the reviewed status without auto-promoting pending mappings."""
        if self.errors or self.configured_status == "pending_review":
            return "pending_review"
        if self.configured_status in {"invalid", "unavailable"}:
            return self.configured_status
        if self.warnings or self.configured_status == "valid_with_warning":
            return "valid_with_warning"
        return "valid"

    @property
    def identity_confirmed(self) -> bool:
        """Return whether the exact table/series/line/frequency gate passed."""
        return not self.errors


def validate_bea_metadata(
    definition: SourceMappingDefinition, rows: list[BeaNipaDataRow]
) -> BeaMetadataValidation:
    """Validate exact identity plus unit and economic semantics from live table rows."""
    result = BeaMetadataValidation(configured_status=definition.mapping_status)
    parameters: dict[str, Any] = definition.source_parameters
    table_name = str(parameters.get("table_name", ""))
    line_number = str(parameters.get("line_number", ""))
    frequency = str(parameters.get("frequency", "")).upper()
    target_multiplier = int(parameters.get("target_unit_multiplier", 0))
    candidate_multiplier = parameters.get("expected_unit_multiplier")

    if str(parameters.get("account_code", "")) != definition.source_series_id:
        result.errors.append("Configured BEA Account/Series Code is internally inconsistent")
    frequency_names = {"A": "annual", "Q": "quarterly", "M": "monthly"}
    if frequency_names.get(frequency) != definition.expected_frequency:
        result.errors.append(
            "Configured BEA frequency does not match the mapping's expected frequency"
        )

    wrong_tables = sorted({row.TableName for row in rows if row.TableName != table_name})
    if wrong_tables:
        result.errors.append(f"TableName mismatch: expected {table_name}, returned {wrong_tables}")

    series_rows = [row for row in rows if row.SeriesCode == definition.source_series_id]
    result.returned_series_codes = sorted({row.SeriesCode for row in rows})
    if not series_rows:
        candidate_line = _line_key(line_number)
        line_codes = sorted(
            {
                row.SeriesCode
                for row in rows
                if candidate_line is not None and _line_key(row.LineNumber) == candidate_line
            }
        )
        result.errors.append(
            "SeriesCode mismatch: expected "
            f"{definition.source_series_id}; candidate line returned {line_codes or 'no series'}"
        )
        return result

    result.returned_line_numbers = sorted({row.LineNumber for row in series_rows})
    expected_line = _line_key(line_number)
    if expected_line is None or any(
        _line_key(row.LineNumber) != expected_line for row in series_rows
    ):
        result.errors.append(
            f"LineNumber mismatch: expected {line_number}, returned "
            f"{result.returned_line_numbers} for {definition.source_series_id}"
        )
        return result

    descriptions = {row.LineDescription for row in series_rows}
    keyword_groups = parameters.get("description_keyword_groups", [])
    for description in descriptions:
        actual = _tokens(description)
        for raw_group in keyword_groups:
            group = {str(item).lower() for item in raw_group}
            if group and not actual.intersection(group):
                result.errors.append(
                    f"LineDescription lacks required economic concept {sorted(group)}"
                )

    expected_kind = str(parameters.get("unit_kind", ""))
    multipliers: set[int] = set()
    for row in series_rows:
        text = f"{row.CL_UNIT} {row.METRIC_NAME}".lower()
        unit_tokens = _tokens(text)
        has_dollars = any(token.startswith("dollar") for token in unit_tokens)
        if expected_kind == "chained_dollars" and ("chained" not in unit_tokens or not has_dollars):
            message = "BEA unit metadata is not chained dollars"
            if message not in result.errors:
                result.errors.append(message)
        elif expected_kind == "chained_dollars" and "2017" not in unit_tokens:
            message = (
                "BEA row unit metadata omits the chained-dollar reference year; "
                "exact table identity and FRED comparison remain required"
            )
            if message not in result.warnings:
                result.warnings.append(message)
        if expected_kind == "index":
            metric_tokens = _tokens(row.METRIC_NAME)
            for raw_group in parameters.get("metric_name_keyword_groups", []):
                group = {str(item).lower() for item in raw_group}
                if group and not metric_tokens.intersection(group):
                    message = f"BEA Metric_Name lacks required price-index concept {sorted(group)}"
                    if message not in result.errors:
                        result.errors.append(message)
            allowed_cl_unit_tokens = {
                str(item).lower() for item in parameters.get("allowed_cl_unit_tokens", [])
            }
            if allowed_cl_unit_tokens and not _tokens(row.CL_UNIT).intersection(
                allowed_cl_unit_tokens
            ):
                message = (
                    "BEA CL_UNIT is incompatible with the configured price-index unit: "
                    f"returned {row.CL_UNIT!r}"
                )
                if message not in result.errors:
                    result.errors.append(message)
        try:
            multiplier = int(row.UNIT_MULT)
            multipliers.add(multiplier)
            BeaAdapter.parse_value(
                row.DataValue or "",
                unit_multiplier=row.UNIT_MULT,
                target_unit_multiplier=target_multiplier,
            )
        except (ValueError, RuntimeError):
            result.errors.append("BEA DataValue/UNIT_MULT cannot be normalized")
    if len(multipliers) > 1:
        result.errors.append(f"BEA returned inconsistent UNIT_MULT values {sorted(multipliers)}")
    elif (
        expected_kind == "index"
        and candidate_multiplier is not None
        and multipliers != {int(candidate_multiplier)}
    ):
        result.errors.append(
            "BEA price-index UNIT_MULT mismatch: "
            f"returned {sorted(multipliers)}, expected {candidate_multiplier}"
        )
    elif candidate_multiplier is not None and multipliers != {int(candidate_multiplier)}:
        result.warnings.append(
            "BEA UNIT_MULT differs from the candidate but is mathematically normalizable: "
            f"returned {sorted(multipliers)}, candidate {candidate_multiplier}"
        )

    period_errors = 0
    for row in series_rows:
        try:
            BeaAdapter.parse_period(row.TimePeriod, frequency=frequency)
        except RuntimeError:
            period_errors += 1
    if period_errors:
        result.errors.append(
            f"{period_errors} BEA periods do not match configured frequency {frequency}"
        )
    if not result.errors and definition.mapping_status == "pending_review":
        result.warnings.append(
            "Exact BEA identity passed; mapping remains pending until BEA/FRED comparison review"
        )
    return result
