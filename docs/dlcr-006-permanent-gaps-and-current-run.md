# DLCR-006: permanent source gaps, revision realignment, and the current-run report

## Why

A current run on 2026-11-14 exposed four ways the system could not stay current:

1. One month that will never exist disabled seven primary signals. BLS did not
   collect the October 2025 household survey or CPI. Stage 3B required every
   period of the trailing window, so the unemployment, U-6, participation,
   employment-population, CPI, core CPI and real retail sales signals failed and
   would have kept failing for the length of their windows (60 or 120 months).
   The labor pillar had no value from 2025-12-16 onward.
2. A refresh after a long pause locked nine indicators. Incremental syncs look
   back a bounded window. After the 2026 annual revisions the primary and the
   FRED fallback each held a different vintage of older periods, the comparison
   raised `source_value_conflict`, and nothing ever closed those events, so the
   resolver refused the indicators even after both sources agreed again.
3. No command computed a current result. The explanation agent only reads
   persisted contexts; the one context in the database had been produced ad hoc.
4. With a single stored context there was never a previous one to compare with.

## What changed

### Permanent-gap authority

`config/data_sources/permanent_gaps.yaml` lists observation periods an official
source has confirmed it will never publish, each with the official evidence URL,
the date the gap became known, and the date it was verified. It is bound to the
source-registry hash and hashed itself (`app/source_registry/permanent_gaps.py`).

Stage 3B uses it in exactly one place, when collecting the trailing window
(`SignalEngine._history_for_target`):

- a declared period with no feature row is skipped;
- a history feature whose only missing inputs are declared periods is skipped;
- every other missing or failed period still fails closed, as before.

Nothing is interpolated or carried forward. A feature whose own formula needs the
missing period still fails for that date. A skipped period sets the
`PERMANENT_GAP_SKIPPED` quality flag and binds the gap authority's version, hash
and the skipped periods into the signal's calculation hash.

Two properties are deliberate:

- **Existing results keep their identity.** The authority is bound into the hash
  only when a gap was actually skipped, so no signal, pillar, state or regime
  that never met a declared gap changes its calculation hash, and no Stage 3
  specification hash changes.
- **No look-ahead in point-in-time mode.** A PIT evaluation only applies gaps
  whose `known_since` is on or before the evaluation date.

Known limit: Stage 3A is untouched, so features that need the missing month as an
input (year-over-year changes through October 2026, for example) still report
`MISSING_INPUT_PERIOD` for the affected dates. Those are supporting signals; no
primary signal is affected at current dates.

### Revision realignment

- `SourceComparisonService.compare` now closes `source_value_conflict` events that
  a complete, persisted comparison no longer reproduces. Bounded and dry-run
  comparisons close nothing; conflicts that still reproduce stay open.
- `CurrentRefreshRunner` accepts `realign_on_conflict`
  (`refresh-current --realign-on-conflict`). When a comparison reports a value
  conflict, or stored primary history has an interior hole that is not a declared
  permanent gap, both sources are re-fetched over their full history and compared
  again. If the conflict remains, the indicator stays blocked.

### Current run and report

- `agents/macro/current_run.py` runs the Stage 3B-3E engines once through the
  existing passive tap, stores the sealed context, skips storing an identical
  result twice, and keeps the last snapshot of the previous month as that month's
  closing record so month-end comparisons have something to compare with.
- `agents/macro/visual_report.py` assembles the report from persisted contexts,
  stored history and stored observations only. Wording and chart selection live
  in `config/reports/us_macro_report_zh.yaml`.
- `python -m app.cli run-macro-report` does refresh, compute, store and render in
  one step and writes `outputs/us_macro_report_<date>.html` and
  `outputs/us_macro_report_latest.html`. `--no-refresh` skips the network refresh;
  `--render-only` renders the newest stored snapshot.
- `python -m app.cli import-macro-history --from <production_results.jsonl>`
  stores a Stage 3F result stream as the history baseline for the charts. Later
  points come from the snapshots each run stores.

### Primary sources that trail the fallback

The DOL weekly-claims report (`r539cy`) leaves its newest weeks blank for two to
three weeks; on 2026-11-14 it carried values through the week ending 2026-10-17
while FRED already had 2026-11-07. The request and parser are correct: the blank
cells are in the source file. The registry deliberately keeps the primary until
it is stale beyond its grace window (21 days for DOL), so the model does not use
the fallback's newer weeks before then. That policy is part of the hashed source
registry and was left unchanged.

The report makes the lag visible instead of hiding it:

- raw-series charts append the fallback's newer periods to the primary series and
  say which part did not come from the primary;
- the reliability section lists every indicator whose fallback is newer than its
  primary, with both dates and the grace window;
- weekly claims are charted as the four-week average, the same shape the model
  uses, and dated by week rather than by month.

Shortening the grace window would change the registry hash and every authority
bound to it, and would make stored snapshots non-comparable with new ones; it
needs its own reviewed registry version.

### Incremental change-index refresh with no new vintage

ALFRED answers a `series/vintagedates` request whose real-time range holds no
vintage date with HTTP 500, not an empty page. The incremental refresh asked for
exactly the uncovered interval (`complete_through + 1` to the evaluation date),
which is empty on most days for most series, so a second run on the following
day retried every indicator through the full backoff and could not build the
lagged trajectory state. Found on 2026-11-15, the day after the first run.

`refresh_change_index` now asks from the last change date the prior generation
already proved. That date must come back, so the answer is never empty and an
outage stays distinguishable from "nothing new". The refresh fails closed if the
anchor is missing or if the provider returns a date inside the interval already
proven complete. The anchor is bound into the new generation's hash.

Still slow under a real ALFRED outage: discovery is retried on every resolver
call for the same indicator and date, so a provider failure costs several
minutes before the run stops.

## Verification on 2026-11-14

- `refresh-current` for all 33 indicators: `succeeded`, `CURRENT_DATA_READY=true`,
  no blockers.
- Current regime: all nine pillars `OK` (labor 11/11, inflation 6/6, real activity
  4/4), growth and inflation data confidence 1.
- `run-macro-report` end to end: about one minute.

Not exercised against live providers: the automatic full-history realignment
path. The same repair was done by hand with the existing backfill commands before
the option existed, and the orchestration is covered by offline tests.

## Operational notes

- The lagged trajectory state is point-in-time and materializes an ALFRED
  information set on first use for a date, so a run needs `FRED_API_KEY`.
- Every stored context is about 1.3 MB of JSONB and the explanation loader
  validates all of them on each load. Identical results are not stored twice, but
  a long daily history will need pruning or a narrower loader query.
- `current_freshness.yaml` still carries a hand-maintained Census M3 release
  calendar; `CURRENT_FRESHNESS_CALENDAR_LOW_HORIZON` warns when it runs short.
