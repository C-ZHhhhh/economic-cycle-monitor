# Stage 3B Direction and Normalization Engine

Stage 3B converts Stage 3A features in real economic units into comparable,
axis-specific signals. It does not decide whether an economic axis is good or
bad, aggregate signals, assign weights, score pillars, or classify regimes.

## Frozen authorities

`config/signals/us_macro_signals.yaml` is the only Stage 3B-v1 signal
specification authority. It is bound to Feature Spec `stage3a-v1`, Source
Registry `stage2b5-v3`, and the frozen registry hash. Every one of the 123
Stage 3A features maps to exactly one signal and one economic axis. Roles remain
33 primary and 90 supporting.

## Production normalization

Stage 3B-v1 uses only `ROLLING_ZSCORE`. For feature observation `t`, the
distribution contains strictly prior, native-frequency feature observations;
the current value never contributes to its own center or scale. Center is the
mean and scale is sample standard deviation (`ddof=1`). Direction is applied
only after normalization.

Windows are 260/156 observations for weekly claims, 120/60 for transformed
monthly features, 60/36 for structural monthly levels, and 40/20 for quarterly
features. The first value is the full window and the second is minimum history.
Between the two, the signal is valid with `PARTIAL_WINDOW`; below minimum it
fails closed. Missing native feature periods are never replaced by older rows.

There is no winsorization or clipping. Absolute z-scores at or above four carry
`EXTREME_VALUE` without changing the value or status. Zero dispersion fails
closed, while small nonzero dispersion remains visible with a reliability flag.

## Current, PIT, and revisions

The signal service consumes only the Stage 3A Feature Engine. Current and PIT
use the same code path. A PIT distribution is built entirely from features
resolved under the same date-EOD `as_of`; current revised history is never mixed
into a historical distribution. Feature revisions may change the raw value,
center, scale, normalized value, directed value, provenance, and hashes.
`REVISION_CHANGED_RESULT` identifies a revised feature result, while
`REVISION_CHANGED_DISTRIBUTION` is reserved for a genuine change in the
prior-history center or scale; revising only the current feature does not claim
that its prior distribution changed.

## Provenance and execution

Results retain the feature calculation hash, every history feature hash, the
history hash, registry/spec identities, history bounds and count, center, scale,
direction, PIT state, flags, and a deterministic calculation hash. Raw provider
payloads are never retained.

Signals are computed on demand. A bounded process-local LRU is isolated by
Current/PIT and feature input hashes. Stage 3B adds no database table or
migration and never reads or writes legacy `derived_features`.
