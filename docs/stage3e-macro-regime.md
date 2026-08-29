# Stage 3E US Macro Regime Engine

Stage 3E consumes only the Stage 3D `MacroStateResult` contract. Its sole
configuration authority is `config/regimes/us_macro_regime.yaml`
(`stage3e-v1`). The authority is bound fail closed to Stage 3D `stage3d-v1`
with hash `8be464960ae16c9ce24f79f99fd13708dcb9be996ab5e8dbdcdc9f14880269c4`
and to the frozen source-registry, feature, normalization, and pillar
authorities inherited through Stage 3D.

## Mechanical level classification

The base taxonomy is a hierarchical three-by-three grid. Growth is classified
as `WEAK`, `NEUTRAL`, or `STRONG`; Inflation Pressure is classified as
`LOW_PRESSURE`, `NEUTRAL_PRESSURE`, or `HIGH_PRESSURE`. Both axes use the exact
inclusive neutral band `[-0.10, +0.10]`. The resulting nine stable IDs describe
only those two mechanical conditions. Display names are separate from IDs.

A complete base regime requires both axis values and an exact minimum confidence
of `2/3` on each axis. A value exactly at either level boundary is neutral, and a
confidence exactly equal to `2/3` is eligible. Missing axes and lower confidence
fail the two-dimensional base regime closed without substituting zero, an older
successful value, or a current value for a historical request.

An otherwise eligible `PARTIAL` MacroState may be classified, but the result
remains `PARTIAL`. Input confidence and distance to a classification boundary
remain separate diagnostics. The margin is the distance to the nearer of the
two axis boundaries; a margin at or below `0.05` adds
`NEAR_REGIME_BOUNDARY` without changing the classification.

## Independent real-time trajectory

Trajectory is a qualifier and never changes the base classification. For an EOD
`t`, Stage 3E requests two independent Stage 3D information sets:

```text
current = MacroState(as_of=t)
lagged  = MacroState(as_of=t minus 3 calendar months)
delta   = current axis value - lagged axis value
```

The subtraction uses calendar months, including month-end and leap-year rules,
not a fixed number of days. Historical endpoints retain their own calculation
hashes, values, confidence, and PIT safety. This is the real-time path: the
lagged state is not recomputed inside the current endpoint's revised information
set.

Growth delta qualifiers are `DETERIORATING`, `STABLE`, and `IMPROVING`.
Inflation Pressure delta qualifiers are `FALLING`, `STABLE`, and `RISING`.
Both use the exact inclusive stable band `[-0.10, +0.10]`. Missing history, low
confidence, PIT safety, and specification failures are reported independently;
they never erase an eligible base regime.

## Context, state, and persistence boundaries

The four Stage 3D Context snapshots pass through unchanged. They cannot modify
an axis condition, base ID, confidence gate, or trajectory threshold. Stage 3E
uses no hysteresis and no process-local prior regime as business authority. Any
transition history can be reconstructed later from an explicit sequence of
`RegimeResult` objects.

Results are compute-on-demand with a bounded copy-safe LRU. Cache identities
include the Stage 3E and Stage 3D specifications, current/PIT evaluation context,
both MacroState calculation hashes, and the lag date. Stage 3E adds no table,
migration, legacy `derived_features` write, HTTP API, historical warehouse,
market rule, portfolio rule, or language-model behavior.
