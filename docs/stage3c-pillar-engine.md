# Stage 3C US Macro Pillar Engine

Stage 3C consumes only Stage 3B `NormalizedSignalResult` objects. It does not read
provider payloads, Stage 2 observations, or Stage 3A feature results, and it does
not select sources, recompute features, normalize values, or change economic
direction. The sole configuration authority is
`config/pillars/us_macro_pillars.yaml` (`stage3c-v1`).

## Frozen taxonomy and aggregation

The engine exposes five core pillars and four contextual pillars. Contextual
pillars are explicitly ineligible for a future core composite. All 33 primary
signals map to exactly one pillar; all 90 supporting signals have a related
pillar, redundancy group, and non-numeric intended use.

Stage 3C-v1 uses `DIRECT_EQUAL_INDICATOR`: each valid primary signal has weight
one. A contribution is `tanh(directed_value / 2)` and is never clipped or rounded
before aggregation. Supporting signals can confirm, diverge, be mixed, or be
insufficient, but never change a pillar value, confidence, or primary weight.

## Coverage, confidence, and semantics

Coverage is the exact valid-primary count divided by the expected-primary count.
Full coverage is `OK`; exact coverage of at least 2/3 is `PARTIAL`; lower positive
coverage is `INSUFFICIENT_COVERAGE`; zero is `NO_VALID_SIGNALS`. Missing inputs
are never replaced by zero. Stage 3C-v1 numeric confidence equals coverage only.
Source health, freshness, supporting agreement, and revision state remain visible
independent diagnostics.

Each pillar is bounded strictly inside `(-1, 1)`. Positive values mean stronger
movement in that pillar's named semantic direction. In particular, positive
inflation means greater inflation pressure, not better macro conditions.

## Information-set contract

Mixed frequencies use `LATEST_KNOWN_SAME_EOD`: the latest native signal result
known by the shared evaluation cutoff is selected without resampling or creating
an observation. If that newest result is an error, the engine does not fall back
to an older successful result. Staleness comes only from upstream contracts.

Historical evaluations require every selected signal to use the same date-EOD
information set and to be PIT-safe. Mixing current and historical results makes
the pillar `PIT_UNSAFE` with a null value. Calculation results retain constituent
dates, values, hashes, specification hashes, and the evaluation information set.

## Persistence and API scope

Pillars are compute-on-demand with a bounded process-local LRU. Stage 3C adds no
database table, Alembic migration, legacy derived-feature write, HTTP endpoint,
composite score, weights, regime rule, or Stage 3D behavior.
