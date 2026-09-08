# US Macro State Explanation Agent v1

## Status and boundary

`agent-explanation-v1` is a presentation authority over the frozen Stage 2 and
Stage 3A–3E deterministic model. It may organize, attribute, compare, summarize,
and explain persisted results. It never rescales, reweights, reclassifies,
forecasts markets, or produces trading signals.

The legacy `MacroCycleResult` remains available and unchanged. It is deprecated
for new explanation work because its probability, nowcast, and provider defaults
are not part of this contract.

## Hard no-I/O contract

The explanation path has no source clients. Snapshot building and deterministic
rendering perform no HTTP request, provider refresh, calendar lookup, ALFRED
request, PIT materialization, or LLM call. They read only locally persisted typed
contexts from `AgentReport` rows whose report type is
`macro_explanation_context`.

Missing current, trajectory, release-EOD, month-end, or revision inputs produce
typed unavailable states. They never trigger upstream computation. In
particular, the explanation service does not call `compute_macro_regime(None)`,
which is known to compute a lagged MacroState and may invoke an on-demand ALFRED
resolver.

## Persisted context and comparisons

A persisted context contains the already computed `RegimeResult`, current and
lagged `MacroStateResult`, all nine `PillarResult` objects, relevant
`NormalizedSignalResult` objects, and mode-appropriate readiness, health, and
revision evidence.

- Previous release EOD is the nearest strictly earlier compatible persisted
  context with cadence `RELEASE_EVENT_EOD`.
- Previous month-end is the nearest strictly earlier compatible persisted
  context with cadence `MONTH_END_EOD`.
- Current and PIT contexts never mix.
- Missing comparisons are `COMPARISON_NOT_AVAILABLE`, never zero.

## Attribution

Growth attribution uses the effective Stage 3D growth-pillar weight multiplied
by the existing Stage 3C primary constituent contribution divided by that
pillar's existing valid primary count. Inflation attribution uses the existing
Inflation Pressure primary contribution divided by its valid primary count.
This is an exact decomposition of the production outputs, not another weighting
model.

The full ranked driver inventory is retained. The presentation projection shows
up to three positive and three negative growth drivers, plus up to three
inflationary and three disinflationary drivers. Ties use ascending `signal_id`.
Contextual pillars and supporting signals never receive core numeric
attribution.

## Confidence, freshness, and revisions

Confidence remains data coverage and calculation reliability. It is never
presented as a probability. Current readiness, current freshness, model
coverage, and model status remain separate fields.

Current-mode revision attribution is explicitly
`NOT_AVAILABLE_FOR_CURRENT_MODE`. PIT mode may carry only canonical
`revision-provenance-v2` evidence. Current readiness and freshness do not apply
to PIT snapshots.

## LLM boundary

The deterministic report is the official fallback and is complete without an
LLM. An LLM can be invoked only by the explicit `explain-macro` CLI or a future
explicit internal job. HTTP read routes never invoke it.

The provider interface is structured and provider-neutral. The model receives
only the allow-listed snapshot and a versioned prompt. Its output must validate
against the narrative schema, every claim must reference existing evidence,
new numeric claims are rejected, and market prediction or investment-advice
language is rejected. Provider, model, prompt, schema, and generation settings
are cache- and provenance-bound. There is no implicit provider fallback.

The current implementation includes only the zero-network mock provider. A live
provider requires a separately reviewed implementation and explicit runtime
configuration.

## Passive upstream context persistence

The optional passive context tap records defensive copies of typed results that
an existing Stage 3B-through-3E orchestration has already returned. Recording
services delegate each call exactly once and return the original result without
changing an economic calculation, authority, cache identity, or hash. The tap
has no method that starts a Stage 3 calculation, refreshes current data, fetches
a provider, or materializes an ALFRED information set.

A completed capture fails closed unless it contains the matching `RegimeResult`,
`MacroStateResult`, trajectory-lag state, exactly nine `PillarResult` objects,
and every primary constituent `NormalizedSignalResult`. Current captures also
require persisted readiness and health evidence. PIT captures reject those
current-only fields. The resulting sealed context is stored through the same
`AgentRun`/`AgentReport` JSONB contract and is the only input subsequently read
by the explanation loader.

## Persistence and interfaces

Validated artifacts reuse `AgentRun` and `AgentReport` JSONB. No migration is
required. The snapshot, deterministic report, optional validated narrative,
authority hashes, evidence references, provider provenance, and git commit are
stored atomically. Credentials, HTTP headers, raw provider payloads, and
unvalidated provider chatter are rejected.

CLI:

- `build-macro-explanation-snapshot --mode current|pit [--as-of YYYY-MM-DD]`
- `render-macro-deterministic-report --mode current|pit [--as-of YYYY-MM-DD]`
- `explain-macro --provider mock --mode current|pit [--as-of YYYY-MM-DD]`

Read-only HTTP:

- `GET /api/v1/macro/explanations/schema`
- `GET /api/v1/macro/explanations/latest`
- `GET /api/v1/macro/explanations/{run_id}`

The HTTP routes only return existing persisted reports.
