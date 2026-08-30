# Stage 3F Historical PIT Validation Framework

Stage 3F is a validation layer, not a new production-model authority. Its only
configuration is `config/validation/us_macro_stage3f.yaml` (`stage3f-v1`). The
loader binds fail closed to the frozen Stage 2 source registry and Stage 3A,
3B, 3C, 3D, and 3E specification versions and hashes. It does not redefine
features, normalizers, pillar weights, macro-state construction, or regime
classification.

## Authorized phase

The current authority permits framework implementation, the coverage audit,
and one bounded pilot. Full historical materialization remains unapproved. The
pilot cannot report a final `PASS`; a normally completed bounded run reports
`PILOT_COMPLETE`, while any budget stop reports `PARTIAL_RUN` with a resumable
checkpoint.

The hard pilot limits are 2,000 actual external ALFRED HTTP attempts (including
retries), 2 GiB incremental validation storage, and approximately eight hours
of wall time. ALFRED concurrency is one. The request guard runs immediately
before transport, so request 2,001 cannot execute. Logical PIT work and actual
HTTP attempts are counted separately.

## Universes and cadence

The framework keeps `FULL_FIXED_UNIVERSE` and
`DYNAMIC_REAL_TIME_UNIVERSE` in separate histories, artifacts, and metrics
namespaces. Both consume production semantic results; Stage 3F does not add a
backtest-only scoring or classification formula.

The canonical path is `RELEASE_EVENT_EOD`; `MONTH_END_EOD` is an independent
benchmark. Same-day candidate releases are coalesced into one date-EOD check.
A canonical event exists only when at least one canonical information-set
semantic hash changes. Retrieval time, computation time, cache refreshes,
request IDs, and audit timestamps do not create events. No intraday sequence is
reconstructed.

## Materialization and replay

The pilot discovers indicator vintage candidates first, reuses exact local PIT
snapshots, requests only exact misses through the DLCR-001 path, coalesces
semantic events by date, then replays the production chain:

```text
Stage 2 date-EOD PIT
  -> Stage 3A Feature Engine
  -> Stage 3B Signal Engine
  -> Stage 3C Pillar Engine
  -> Stage 3D Macro State
  -> Stage 3E Macro Regime
```

The three-calendar-month trajectory lag endpoint is materialized from the same
historical cache even when it is not itself a canonical release event.
Sensitivity and hysteresis research operate only on cached semantic outputs and
cannot trigger provider requests.

## Coverage evidence

The audit records these ordered boundaries at date-EOD granularity:

- `RAW_PIT_START`
- `FEATURE_PIT_START`
- `SIGNAL_PIT_START`
- `PILLAR_PIT_START`
- `MACRO_STATE_PIT_START`
- `REGIME_LEVEL_PIT_START`
- `REGIME_TRAJECTORY_PIT_START`

A discovered boundary stores the first successful candidate, the immediately
previous tested candidate and status, source evidence, production authority
hashes, and calculation hashes. This proves the first eligible tested candidate
rather than merely the first arbitrary date sampled. The known 2014-05-15
Manufacturing Capacity vintage boundary is not hard-coded as the full-model
start.

## Checkpoint and artifacts

The atomic checkpoint is `outputs/stage3f/checkpoint.json`. It is bound to the
validation hash, ordered production hashes, universe, and schedule hash. A
binding change fails closed. Runtime timestamps may be present in the
checkpoint but never enter the deterministic history replay hash.

All run products stay under the gitignored `outputs/stage3f/` directory. The
approved formats are JSON, streaming JSONL, CSV, and Markdown. Production and
counterfactual namespaces are distinct. Parquet, PyArrow, database validation
tables, historical warehouses, migrations, and committed historical datasets
are not part of v1.

## Hard quality gates

The pilot stops after preserving first evidence for future leakage, production
authority drift, deterministic replay mismatch, a classified PIT-unsafe result,
current/lag same-as-of violation, or credential leakage. Correct fail-closed
historical `OUTSIDE_COVERAGE`, `NO_VINTAGE`, and partial results are historical
limitations rather than implementation failures.

Transition counts use only a change in `base_regime_id` for the base transition.
Growth condition, inflation condition, growth trajectory, and inflation
trajectory transitions are reported independently. Event-count occupancy and
calendar-duration occupancy are also separate metrics.

## CLI

The authority can be checked without database or network access:

```bash
python -m app.cli validate-stage3f-config --json
```

The live pilot is deliberately a separate manually invoked command:

```bash
python -m app.cli run-stage3f-pilot --json
```

It never escalates into the provisional full-run budget. A full historical run
requires a second explicit approval after the pilot report documents actual
request amplification, reuse, storage growth, runtime, and projected full-run
resources.
