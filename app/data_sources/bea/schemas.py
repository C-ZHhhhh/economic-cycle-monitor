"""Typed BEA metadata, NIPA data, and request-audit schemas."""

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


class BeaDataset(BaseModel):
    """One BEA API dataset descriptor."""

    model_config = ConfigDict(extra="ignore")

    DatasetName: str
    DatasetDescription: str = ""


class BeaParameter(BaseModel):
    """One BEA dataset request-parameter descriptor."""

    model_config = ConfigDict(extra="ignore")

    ParameterName: str
    ParameterDescription: str = ""
    ParameterIsRequiredFlag: str | None = None
    MultipleAcceptedFlag: str | None = None
    AllValue: str | None = None


class BeaParameterValue(BaseModel):
    """One permissible BEA parameter value."""

    model_config = ConfigDict(extra="ignore")

    Key: str
    Desc: str = ""
    FirstAnnualYear: str | None = None
    FirstMonthlyYear: str | None = None
    FirstQuarterlyYear: str | None = None
    LastAnnualYear: str | None = None
    LastMonthlyYear: str | None = None
    LastQuarterlyYear: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_parameter_value_fields(cls, value: Any) -> Any:
        """Normalize BEA parameter-specific keys into one internal pair."""
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "Key" not in normalized:
            for alias in ("TableName", "FrequencyID", "Frequency", "Year"):
                if alias in normalized:
                    normalized["Key"] = normalized[alias]
                    break
        if "Desc" not in normalized and "Description" in normalized:
            normalized["Desc"] = normalized["Description"]
        return normalized


class BeaNipaDataRow(BaseModel):
    """One unnormalized row returned by a NIPA table request."""

    model_config = ConfigDict(extra="ignore")

    TableName: str
    SeriesCode: str
    LineNumber: str
    LineDescription: str
    TimePeriod: str
    METRIC_NAME: str = ""
    CL_UNIT: str
    UNIT_MULT: str
    DataValue: str | None = None
    NoteRef: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_metric_name_alias(cls, value: Any) -> Any:
        """Accept documented BEA casing variants without weakening other fields."""
        if isinstance(value, dict) and "METRIC_NAME" not in value:
            normalized = dict(value)
            for alias in ("Metric_Name", "MetricName"):
                if alias in normalized:
                    normalized["METRIC_NAME"] = normalized[alias]
                    break
            return normalized
        return value


class BeaNote(BaseModel):
    """One BEA note referenced by NIPA data rows."""

    model_config = ConfigDict(extra="ignore")

    NoteRef: str
    NoteText: str = ""


class BeaPayload(BaseModel):
    """Credential-free evidence for one BEA request."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    endpoint: str
    sanitized_parameters: dict[str, Any]
    body: dict[str, Any]
    started_at: datetime
    finished_at: datetime
    status_code: int
    attempt_count: int
    response_hash: str
    response_path: Path | None
    from_cache: bool
