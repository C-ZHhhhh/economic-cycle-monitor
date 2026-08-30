"""Streaming, gitignored Stage 3F artifact writers."""

import csv
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel

from app.validation.hashing import semantic_hash


class ArtifactWriter:
    """Write approved JSON, JSONL, CSV, and Markdown namespaces only."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, namespace: str, name: str) -> Path:
        if namespace not in {"production", "counterfactual", "provenance", "reports"}:
            raise ValueError("Unsupported Stage 3F artifact namespace")
        path = self.root / namespace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _serializable(value: object) -> object:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        return value

    def write_json(self, namespace: str, name: str, value: object) -> tuple[Path, str]:
        """Write stable human-readable JSON."""
        path = self._path(namespace, name)
        payload = (
            json.dumps(self._serializable(value), indent=2, sort_keys=True, default=str) + "\n"
        )
        path.write_text(payload, encoding="utf-8")
        return path, semantic_hash(json.loads(payload))

    def append_jsonl(self, namespace: str, name: str, value: object) -> Path:
        """Append one provenance record without buffering historical datasets."""
        path = self._path(namespace, name)
        payload = json.dumps(
            self._serializable(value), sort_keys=True, separators=(",", ":"), default=str
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        return path

    def write_csv(
        self,
        namespace: str,
        name: str,
        rows: Sequence[Mapping[str, object]],
        *,
        fieldnames: Sequence[str] | None = None,
    ) -> tuple[Path, str]:
        """Write a deterministic tabular artifact."""
        path = self._path(namespace, name)
        columns = list(fieldnames or (rows[0].keys() if rows else []))
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            if columns:
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: row.get(key) for key in columns})
        return path, semantic_hash([dict(row) for row in rows])

    def write_markdown(self, name: str, lines: Iterable[str]) -> tuple[Path, str]:
        """Write a local validation report in the report namespace."""
        path = self._path("reports", name)
        payload = "\n".join(lines).rstrip() + "\n"
        path.write_text(payload, encoding="utf-8")
        return path, semantic_hash(payload)


def directory_size(path: Path) -> int:
    """Return regular-file bytes without following directory symlinks."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def storage_snapshot(paths: Mapping[str, Path]) -> dict[str, int]:
    """Measure raw, cache, and validation-output bytes separately."""
    return {name: directory_size(path) for name, path in paths.items()}


def artifact_hashes(paths: Iterable[Path]) -> dict[str, str]:
    """Hash completed artifact bytes for checkpoint evidence."""
    return {str(path): semantic_hash(path.read_bytes().hex()) for path in paths if path.is_file()}
