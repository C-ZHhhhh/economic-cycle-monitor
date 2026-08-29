"""Static Stage 3F scope and production-path architecture regressions."""

import inspect
from pathlib import Path

from app.validation.service import HistoricalValidationService


def test_stage3f_service_uses_production_stage3_engines() -> None:
    source = inspect.getsource(HistoricalValidationService)
    for engine in (
        "FeatureEngine",
        "SignalEngine",
        "PillarEngine",
        "MacroStateEngine",
        "MacroRegimeEngine",
    ):
        assert engine in source


def test_stage3f_has_no_backtest_specific_classifier_or_formula() -> None:
    source = Path("app/validation/service.py").read_text(encoding="utf-8")
    assert "classify_growth(" not in source
    assert "classify_inflation(" not in source
    assert "evaluate_formula(" not in source


def test_stage3f_service_exposes_pilot_but_no_full_run_method() -> None:
    methods = set(dir(HistoricalValidationService))
    assert "run_pilot" in methods
    assert "run_full" not in methods
    assert "backfill" not in methods


def test_stage3f_creates_no_database_model_or_migration() -> None:
    package_source = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("app/validation").glob("*.py")
    )
    assert "mapped_column(" not in package_source
    assert "Base.metadata" not in package_source
    assert not list(Path("migrations/versions").glob("*stage3f*"))


def test_stage3f_artifacts_are_gitignored() -> None:
    ignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "outputs/*" in ignore
    assert "data/raw/" in ignore
    assert "data/cache/" in ignore


def test_stage3f_does_not_import_or_invoke_llm_code() -> None:
    package_source = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("app/validation").glob("*.py")
    )
    assert "app.llm" not in package_source
    assert "DeepSeek" not in package_source
