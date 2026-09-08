"""Deterministic rendering for macro explanation snapshots."""

from __future__ import annotations

from agents.macro.explanation_config import hash_payload
from agents.macro.explanation_schemas import (
    DependencyAvailability,
    DeterministicDriver,
    DeterministicMacroExplanationReport,
    MacroExplanationSnapshot,
)


def _driver_line(driver: DeterministicDriver) -> str:
    return f"{driver.signal_id} ({driver.pillar_id}): attributed impact {driver.attributed_impact}"


def _change_lines(snapshot: MacroExplanationSnapshot) -> tuple[str, ...]:
    lines: list[str] = []
    for label, comparison in (
        ("previous release EOD", snapshot.previous_release_eod),
        ("previous month-end", snapshot.previous_month_end),
    ):
        if comparison.status is not DependencyAvailability.AVAILABLE:
            lines.append(f"{label}: {comparison.status.value}")
            continue
        changed = [
            item
            for item in comparison.changes
            if item.previous_hash != item.current_hash
            or item.previous_status != item.current_status
        ]
        if not changed:
            lines.append(f"{label} {comparison.previous_evaluation_as_of}: no typed changes")
            continue
        for item in changed:
            detail = (
                f"delta {item.delta}"
                if item.delta is not None
                else f"status {item.previous_status} -> {item.current_status}"
            )
            lines.append(
                f"{label} {comparison.previous_evaluation_as_of}: "
                f"{item.entity_type.value} {item.entity_id} "
                f"{item.change_type.value}, {detail}"
            )
    return tuple(lines)


def render_deterministic_report(
    snapshot: MacroExplanationSnapshot,
) -> DeterministicMacroExplanationReport:
    """Render a complete stable report using only canonical snapshot facts."""
    regime = snapshot.base_regime_id or "UNCLASSIFIED"
    summary = (
        f"As of {snapshot.evaluation_as_of}, the frozen model reports {regime}; "
        f"growth condition {snapshot.growth_condition.value if snapshot.growth_condition else 'N/A'} "
        f"and inflation condition "
        f"{snapshot.inflation_condition.value if snapshot.inflation_condition else 'N/A'}."
    )
    driver_by_id = {
        item.driver_id: item
        for item in (*snapshot.drivers.full_growth, *snapshot.drivers.full_inflation)
    }
    ordered_driver_ids = (
        *snapshot.drivers.top_positive_growth,
        *snapshot.drivers.top_negative_growth,
        *snapshot.drivers.top_inflationary,
        *snapshot.drivers.top_disinflationary,
    )
    primary_drivers = tuple(_driver_line(driver_by_id[item]) for item in ordered_driver_ids)
    confidence_note = (
        f"Growth data confidence is {snapshot.growth_data_confidence}; inflation data "
        f"confidence is {snapshot.inflation_data_confidence}; minimum axis confidence is "
        f"{snapshot.minimum_axis_confidence}. These are coverage and calculation-reliability "
        "measures, not probabilities."
    )
    freshness_note = (
        "Current freshness is not applicable to historical PIT evaluation."
        if snapshot.readiness is None
        else (
            f"CURRENT_DATA_READY={str(snapshot.readiness.current_data_ready).lower()}; "
            f"current blockers={len(snapshot.readiness.current_blockers)}."
        )
    )
    context = tuple(snapshot.contextual_diagnostics)
    changes = _change_lines(snapshot)
    markdown_lines = [
        "# US Macro State — Deterministic Explanation",
        "",
        summary,
        "",
        "## What changed",
        "",
        *(f"- {item}" for item in changes),
        "",
        "## Primary drivers",
        "",
        *(f"- {item}" for item in primary_drivers),
        "",
        "## Context",
        "",
        *(f"- {item}" for item in context),
        "",
        "## Confidence and freshness",
        "",
        confidence_note,
        "",
        freshness_note,
        "",
        snapshot.revision.note,
    ]
    if snapshot.warnings:
        markdown_lines.extend(["", "## Warnings", "", *(f"- {item}" for item in snapshot.warnings)])
    markdown = "\n".join(markdown_lines).rstrip() + "\n"
    payload = {
        "snapshot_calculation_hash": snapshot.snapshot_calculation_hash,
        "summary": summary,
        "what_changed": changes,
        "primary_drivers": primary_drivers,
        "contextual_signals": context,
        "confidence_note": confidence_note,
        "freshness_note": freshness_note,
        "revision_note": snapshot.revision.note,
        "warnings": snapshot.warnings,
        "markdown": markdown,
    }
    return DeterministicMacroExplanationReport(
        **payload,
        report_hash=hash_payload(payload),
    )
