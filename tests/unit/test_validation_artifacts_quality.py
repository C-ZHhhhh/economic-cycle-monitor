"""Stage 3F artifact namespace, storage, and leakage tests."""

import json
from datetime import date
from pathlib import Path

import pytest

from app.validation.artifacts import ArtifactWriter, directory_size, storage_snapshot
from app.validation.quality import contains_credential, future_dates, leakage_evidence


def test_artifact_writer_separates_production_and_counterfactual_namespaces(
    tmp_path: Path,
) -> None:
    writer = ArtifactWriter(tmp_path)
    production, _ = writer.write_json("production", "history.json", {"value": 1})
    counterfactual, _ = writer.write_json("counterfactual", "history.json", {"value": 2})
    assert production != counterfactual
    assert production.parent.name == "production"
    assert counterfactual.parent.name == "counterfactual"


def test_artifact_writer_streams_jsonl(tmp_path: Path) -> None:
    writer = ArtifactWriter(tmp_path)
    path = writer.append_jsonl("provenance", "events.jsonl", {"event": 1})
    writer.append_jsonl("provenance", "events.jsonl", {"event": 2})
    assert [json.loads(line)["event"] for line in path.read_text().splitlines()] == [1, 2]


def test_artifact_writer_supports_csv_and_markdown_without_parquet(tmp_path: Path) -> None:
    writer = ArtifactWriter(tmp_path)
    csv_path, _ = writer.write_csv("production", "table.csv", [{"a": 1}, {"a": 2}])
    report, _ = writer.write_markdown("pilot.md", ["# Pilot", "PASS"])
    assert csv_path.read_text().splitlines() == ["a", "1", "2"]
    assert report.read_text() == "# Pilot\nPASS\n"
    assert not list(tmp_path.rglob("*.parquet"))


def test_unknown_artifact_namespace_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="namespace"):
        ArtifactWriter(tmp_path).write_json("mixed", "bad.json", {})


def test_storage_snapshot_counts_regular_file_bytes(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    cache = tmp_path / "cache"
    raw.mkdir()
    cache.mkdir()
    (raw / "a").write_bytes(b"123")
    (cache / "b").write_bytes(b"12345")
    assert directory_size(raw) == 3
    assert storage_snapshot({"raw": raw, "cache": cache}) == {"raw": 3, "cache": 5}


def test_future_leakage_detector_returns_first_semantic_date() -> None:
    as_of = date(2026, 1, 2)
    assert future_dates(as_of, [date(2026, 1, 1), date(2026, 1, 3)]) == [date(2026, 1, 3)]
    evidence = leakage_evidence(
        evaluation_as_of=as_of, semantic_dates=[date(2026, 1, 1), date(2026, 1, 3)]
    )
    assert evidence is not None
    assert evidence.gate == "FUTURE_LEAKAGE"


def test_no_leakage_is_not_a_hard_failure() -> None:
    assert (
        leakage_evidence(evaluation_as_of=date(2026, 1, 2), semantic_dates=[date(2026, 1, 2), None])
        is None
    )


def test_credential_detector_reports_presence_without_echoing_value() -> None:
    secret = "test-secret-value"
    assert contains_credential("safe payload test-secret-value", secret)
    assert contains_credential('{"api_key":"redacted"}')
    assert not contains_credential("credential_configured=true", secret)
