# Stage 3D US Macro State Engine

Stage 3D consumes only the complete same-EOD set of nine Stage 3C
`PillarResult` objects. Its sole configuration authority is
`config/composites/us_macro_state.yaml` (`stage3d-v1`). The authority is bound
fail closed to source registry `stage2b5-v3`, feature spec `stage3a-v1`,
normalization spec `stage3b-v1`, and pillar spec `stage3c-v1` with pillar hash
`fed386805496d63cc7dd1ed0b6f772a8f821042fc9b04d93884d6c008f6b3d41`.

## Three-layer continuous state

`US_MACRO__STATE_VECTOR` contains three semantically distinct layers:

1. `US_MACRO__GROWTH_STRENGTH`, built from Real Activity, Labor,
   Manufacturing, and Housing.
2. The existing `US_MACRO__INFLATION_PRESSURE` pillar, referenced directly
   without inversion, renormalization, or a second numeric alias.
3. A context vector containing Household Financial Cushion, Credit Usage
   Pressure, Inventory Accumulation Pressure, and Inventory Absorption Strength.

There is no macro health score, five-core mean, growth-minus-inflation score,
0-100 headline, context score, regime label, threshold, recession probability,
policy penalty, portfolio rule, or LLM behavior.

## Growth calculation

Each expected Growth pillar has immutable base weight `0.25`. With `V` equal to
the usable pillars, Stage 3D computes:

```text
available_base_weight = sum(base_weight[i] for i in V)
effective_weight[i] = base_weight[i] / available_base_weight  if i in V else 0
growth_strength = sum(base_weight[i] * pillar_value[i] for i in V)
                  / available_base_weight
```

Four or three valid pillars permit a value; fewer than three fail closed as
`INSUFFICIENT_GROWTH_COVERAGE`. Missing values are not zero or neutral. Four
complete pillars produce `OK`; three pillars or any used `PARTIAL` pillar produce
`PARTIAL`.

Growth confidence retains the expected four-pillar denominator:

```text
growth_confidence = sum(base_weight[i] * usable_pillar_confidence[i])
```

An unusable pillar contributes zero confidence but never a zero score. Confidence
does not alter score weights. Stage 3D applies no second tanh, clipping, softsign,
winsorization, or rounding; the weighted mean remains strictly inside `(-1, 1)`.

Signed breadth is `(positive_count - negative_count) / valid_count`, with neutral
defined only as exact zero. Dispersion is the population standard deviation over
valid Growth pillars. Both remain diagnostics and do not change the value or
confidence.

## Inflation and context

Inflation preserves the Stage 3C value, confidence, status, semantic direction,
and calculation hash. Higher always means greater inflation pressure.

The four context pillars remain independent typed snapshots. They are never
averaged and never correct Growth. Missing context adds `CONTEXT_PARTIAL` and can
make the overall state `PARTIAL`, but it does not erase valid Growth or Inflation.
Missing Inflation produces `MISSING_INFLATION` while retaining valid Growth.

## Current, PIT, and coverage behavior

Current and historical calculations use the same aggregation path. All nine
pillars must have exactly the same `evaluation_as_of`; every historical input
must be PIT-safe. A spec mismatch, as-of mismatch, or unsafe historical input
fails the whole state closed. Current and PIT cache identities are separate and
include all nine ordered Stage 3C calculation hashes.

The 2008-12-31 information set has three usable Growth pillars. It produces
Growth near `-0.742954`, coverage `0.75`, confidence `0.70`, `PARTIAL` status,
and `HISTORICAL_COVERAGE_LIMITATION`. The unavailable Labor pillar is not
replaced by current, revised, or older successful data.

The full 2020-05-31 control produces Growth near `-0.782084`. The full
2022-07-31 control preserves Growth near `-0.084019` and Inflation Pressure near
`+0.799399`. These controls validate logic and information preservation; they do
not tune weights. The exact earliest complete primary PIT universe remains
`NOT_YET_MATERIALIZED` and is deferred to Stage 3F.

## Persistence and API scope

Macro states are compute-on-demand with a bounded process-local LRU. Stage 3D
adds no table, migration, legacy `derived_features` write, HTTP endpoint,
historical warehouse, or Stage 3E classifier.
