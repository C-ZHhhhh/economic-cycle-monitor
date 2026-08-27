# Stage 3A US Macro Feature Engine

## Scope and authority

Stage 3A transforms canonical observations into economically meaningful, native-frequency
features. It does not select sources, normalize, assign direction, score, weight, classify
regimes, call an LLM, or expose an HTTP endpoint.

`config/features/us_macro_features.yaml` is the only feature-selection authority. Version
`stage3a-v1` binds exactly to source registry `stage2b5-v3` and its frozen SHA-256 hash.
The configuration dynamically matches all 33 registry indicators, contains exactly one
primary per indicator, and uses globally unique stable feature IDs. It contains no provider
identity, fallback decision, weight, direction, normalization, or executable expression.

Formula implementations live in the closed `stage3a-formulas-v1` typed registry. YAML may
only reference a registered formula and an integer window.

## Architecture

The Python domain API exposes `compute_feature`, `compute_indicator_features`, and
`compute_feature_set`. The last two group work by indicator so all selected features share
one canonical resolver call and one bounded observation list.

Current and PIT computation use the same path:

1. Validate the active feature catalog against the frozen source registry version/hash.
2. Extend the requested start by the maximum native-period formula lookback.
3. Resolve observations only through the Stage 2 canonical resolver. A PIT request uses
   DLCR-001 on-demand exact-date ALFRED materialization.
4. Index the small canonical series by actual observation date.
5. Enumerate every required weekly, monthly, or quarterly date for each target.
6. Return a value or an explicit fail-closed status with complete provenance.

No provider table or raw payload is read. No feature table or migration is created, and the
legacy `derived_features` table is not read or written.

## Formula registry

- `LEVEL`: current canonical value.
- `DIFF_K` and `POINT_DIFF_K`: `x_t - x_(t-k)`.
- `AVG_PERIOD_DIFF_K`: `(x_t - x_(t-k)) / k`.
- `PCT_CHANGE_K`: `100 * (x_t / x_(t-k) - 1)`.
- `MONTHLY_ANNUALIZED_K`: `100 * ((x_t / x_(t-k)) ** (12/k) - 1)`; `k` must divide 12.
- `MONTHLY_YOY`: `100 * (x_t / x_(t-12) - 1)`.
- `QUARTERLY_QOQ_ANNUALIZED`: `100 * ((x_t / x_(t-1)) ** 4 - 1)`.
- `QUARTERLY_YOY`: `100 * (x_t / x_(t-4) - 1)`.
- `MOVING_AVERAGE_K`: trailing arithmetic mean of `k` native periods.
- `MA3_BLOCK_GROWTH_ANNUALIZED`: annualized growth between adjacent non-overlapping
  three-month averages.
- `GROWTH_ACCEL_QOQ_1Q`: current QoQ annualized growth minus the prior quarter's QoQ
  annualized growth.
- `GROWTH_ACCEL_3M_NONOVERLAP`: recent three-month annualized growth minus the preceding
  non-overlapping three-month annualized growth.
- `MA4W_CHANGE_KW`: trailing four-week average minus the trailing four-week average ending
  `k` weeks earlier.
- `MA4W_YOY_PCT`: percentage change between trailing four-week averages 52 weeks apart.

Compound formulas require a positive denominator and are forbidden by configuration for
canonical percent, rate, and ratio units. Hires and quits point changes use `rate_points`;
inventory-sales ratio changes use `ratio_points`. Intermediate Decimal precision is 34 and
the engine does not quantize or apply presentation rounding.

## Continuity, missing data, and PIT

Weekly inputs must be exactly seven days apart. Monthly and quarterly formulas use calendar
month and calendar quarter shifts. Every intermediate period is required even when a
formula only uses endpoints. Missing history and missing interior periods are distinct
statuses. The engine never skips a row, forward-fills, interpolates, centers a rolling
window, or creates monthly/weekly copies of lower-frequency data.

PIT inputs must all carry `is_point_in_time_safe = true`; otherwise the result is
`PIT_UNSAFE` with no value. Feature targets and inputs never extend past `as_of`. A revised
exact snapshot may change a historical feature and is marked `REVISION_CHANGED_RESULT`
when observed by the same engine process.

## Provenance, quality, and cache

Every result contains the feature and formula versions, indicator, target/as-of dates,
value/unit, exact input count and dates, source identities, mapping versions, release and
retrieval timestamps, vintage dates, source policy version, registry hash, PIT safety,
status, quality flags, and a deterministic calculation hash. Raw payloads are excluded.

The only cache is a 256-entry process-local LRU. Its key separates current/PIT and includes
feature/formula versions, indicator, requested range, as-of date, source policy version,
registry hash, and a hash of canonical resolver inputs. A revision changes the input hash
and therefore cannot reuse an older calculation. There is no Redis or durable feature
store.
