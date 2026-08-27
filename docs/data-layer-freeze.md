# Stage 2B-5 Data Layer Freeze Contract

Registry schema `1.0.0` and policy version `stage2b5-v3` freeze the current-data
contract. A deterministic SHA-256 hash is calculated from the validated registry payload
at runtime and recorded in seed audit status.

Policy v2 separates Census MTIS from generic Census monthly freshness. MTIS evaluates
the official data-period release timestamp in `America/New_York`, starts grace only after
that timestamp, and degrades rather than inventing a hard stale result when a future
official calendar entry is unavailable.

Policy v3 applies the same exact-calendar precedence independently to BEA Personal
Income and Outlays, BLS JOLTS, and Federal Reserve G.19. Each family preserves its
official `America/New_York` release time and existing grace duration; approximate rules
remain metadata only and cannot trigger hard stale while an exact future date is unknown.

## Authority and inventory

- `config/indicators/us_macro.yaml` owns economic concepts and canonical frequency,
  unit, seasonal adjustment, base year, and FRED reference identity.
- Provider YAML owns transport identity, dimensions, raw units, normalization, and
  mapping evidence. It does not own resolution policy.
- `config/data_sources/source_registry.yaml` is the only authority for primary,
  fallback, vintage, history, freshness, revision, comparison, and resolution policy.
- PostgreSQL is materialized state, observations, runtime evidence, and immutable audit;
  it is never the policy authority.

The inventory is exactly 33 indicators: BLS 14, BEA 5, Census 6, Federal Reserve 4,
DOL 2, and FRED current 2 primary series.

| Indicator | Primary identity | Fallback | Type | Earliest primary | PIT |
| --- | --- | --- | --- | --- | --- |
| US_REAL_GDP | BEA `A191RX` | FRED_CURRENT | DIRECT | 1947-01-01 | PIT_SAFE_DATE_EOD |
| US_REAL_PCE | BEA `DPCERX` | FRED_CURRENT | DIRECT | 2007-01-01 | PIT_SAFE_DATE_EOD |
| US_REAL_DISPOSABLE_INCOME | BEA `A067RX` | FRED_CURRENT | DIRECT | 1959-01-01 | PIT_SAFE_DATE_EOD |
| US_INDUSTRIAL_PRODUCTION | FED `IP.B50001.S` | FRED_CURRENT | DIRECT | 1919-01-01 | PIT_SAFE_DATE_EOD |
| US_REAL_RETAIL_SALES | FRED `RRSFS` | — | DERIVED | 1992-01-01 | PIT_SAFE_DATE_EOD |
| US_NONFARM_EMPLOYMENT | BLS `CES0000000001` | FRED_CURRENT | DIRECT | 1939-01-01 | PIT_SAFE_DATE_EOD |
| US_CPI | BLS `CUSR0000SA0` | FRED_CURRENT | DIRECT | 1947-01-01 | PIT_SAFE_DATE_EOD |
| US_CORE_CPI | BLS `CUSR0000SA0L1E` | FRED_CURRENT | DIRECT | 1957-01-01 | PIT_SAFE_DATE_EOD |
| US_PCE_PRICE | BEA `DPCERG` | FRED_CURRENT | DIRECT | 1959-01-01 | PIT_SAFE_DATE_EOD |
| US_CORE_PCE_PRICE | BEA `DPCCRG` | FRED_CURRENT | DIRECT | 1959-01-01 | PIT_SAFE_DATE_EOD |
| US_PPI_COMMODITIES | BLS `WPU00000000` | FRED_CURRENT | DIRECT | 1913-01-01 | PIT_SAFE_DATE_EOD |
| US_UNEMPLOYMENT_RATE | BLS `LNS14000000` | FRED_CURRENT | DIRECT | 1948-01-01 | PIT_SAFE_DATE_EOD |
| US_U6_RATE | BLS `LNS13327709` | FRED_CURRENT | DIRECT | 1994-01-01 | PIT_SAFE_DATE_EOD |
| US_PARTICIPATION_RATE | BLS `LNS11300000` | FRED_CURRENT | DIRECT | 1948-01-01 | PIT_SAFE_DATE_EOD |
| US_EMPLOYMENT_POP_RATIO | BLS `LNS12300000` | FRED_CURRENT | DIRECT | 1948-01-01 | PIT_SAFE_DATE_EOD |
| US_INITIAL_CLAIMS | DOL `r539cy:initial_claims:sa` | FRED_CURRENT | DIRECT | 1967-01-07 | PIT_SAFE_DATE_EOD |
| US_CONTINUING_CLAIMS | DOL `r539cy:insured_unemployment:sa` | FRED_CURRENT | DIRECT | 1967-01-07 | PIT_SAFE_DATE_EOD |
| US_JOB_OPENINGS | BLS `JTS000000000000000JOL` | FRED_CURRENT | DIRECT | 2000-12-01 | PIT_SAFE_DATE_EOD |
| US_HIRES | BLS `JTS000000000000000HIR` | FRED_CURRENT | DIRECT | 2000-12-01 | PIT_SAFE_DATE_EOD |
| US_QUITS | BLS `JTS000000000000000QUR` | FRED_CURRENT | DIRECT | 2000-12-01 | PIT_SAFE_DATE_EOD |
| US_TEMP_HELP_EMPLOYMENT | BLS `CES6056132001` | FRED_CURRENT | DIRECT | 1990-01-01 | PIT_SAFE_DATE_EOD |
| US_MANUFACTURING_HOURS | BLS `CES3000000007` | FRED_CURRENT | DIRECT | 1939-01-01 | PIT_SAFE_DATE_EOD |
| US_AVERAGE_HOURLY_EARNINGS | BLS `CES0500000008` | FRED_CURRENT | DIRECT | 1964-01-01 | PIT_SAFE_DATE_EOD |
| US_SAVING_RATE | FRED `PSAVERT` | — | DIRECT | 1959-01-01 | PIT_SAFE_DATE_EOD |
| US_CONSUMER_CREDIT | FED `DTCTL.M` | FRED_CURRENT | DIRECT | 1943-01-01 | PIT_SAFE_DATE_EOD |
| US_REVOLVING_CREDIT | FED `DTCTLR.M` | FRED_CURRENT | DIRECT | 1968-01-01 | PIT_SAFE_DATE_EOD |
| US_DURABLE_GOODS_ORDERS | CENSUS `m3:MDM:NO` | FRED_CURRENT | DIRECT | 1992-01-01 | PIT_SAFE_DATE_EOD |
| US_CORE_CAPITAL_GOODS_ORDERS | CENSUS `m3:NXA:NO` | FRED_CURRENT | DIRECT | 1992-01-01 | PIT_SAFE_DATE_EOD |
| US_MANUFACTURING_CAPACITY | FED `CAPUTL.B00004.S` | FRED_CURRENT | DIRECT | 1948-01-01 | PIT_SAFE_DATE_EOD |
| US_HOUSING_STARTS | CENSUS `resconst:ASTARTS:TOTAL` | FRED_CURRENT | DIRECT | 1959-01-01 | PIT_SAFE_DATE_EOD |
| US_BUILDING_PERMITS | CENSUS `resconst:APERMITS:TOTAL` | FRED_CURRENT | DIRECT | 1960-01-01 | PIT_SAFE_DATE_EOD |
| US_BUSINESS_INVENTORIES | CENSUS `mtis:TOTBUS:IM` | FRED_CURRENT | DIRECT | 1992-01-01 | PIT_SAFE_DATE_EOD |
| US_INVENTORY_SALES_RATIO | CENSUS `mtis:TOTBUS:IR` | FRED_CURRENT | DIRECT | 1992-01-01 | PIT_SAFE_DATE_EOD |

All 14 BLS boundaries were verified with limited official BLS Public Data API probes of
the candidate first year and its predecessor. No FRED date was substituted and no history
was refreshed. RRSFS remains FRED-derived; Census `44X72/SM` is a supporting nominal
component that cannot be primary, fallback target, RRSFS observation, or direct comparison.

## Resolver, fallback, and stitching

Resolution evaluates registry eligibility, mapping quality, provider and indicator health,
expected period, grace, and observation presence. Only
`PRIMARY_BEFORE_OFFICIAL_HISTORY`, `PRIMARY_STALE_BEYOND_GRACE`, and
`PRIMARY_TEMPORARILY_UNAVAILABLE` may activate fallback. Identity, value,
normalization, or seasonal conflicts fail closed; comparison never chooses a value.

Recovery requires valid identity, restored expected period, one complete successful sync,
and conflict-free overlap. Early-history stitching must be explicitly enabled and validate
frequency, unit, scale, seasonal adjustment, base, and overlap. Unexplained level breaks
reject the transition. All current policies are `PRIMARY_ONLY`.

## Freshness, health, revision, and comparison

Freshness uses expected latest period plus an official schedule model and grace. Legacy
maximum staleness is only a ceiling; official-to-FRED distribution lag is not release lag.
Mapping quality (`VALID`, `VALID_WITH_METADATA_WARNING`, `PENDING_REVIEW`, `INVALID`,
`UNAVAILABLE`) is separate from provider and indicator runtime health (`HEALTHY`,
`DEGRADED`, `PARTIAL`, `STALE`, `UNAVAILABLE`, `ERROR`). DOL mapping identity is
`VALID`; temporary official lag is runtime `DEGRADED`.

Revision policy separately versions recent, annual/benchmark, and deep reconciliation.
CES, JOLTS, ICSA, and CCSA annual policies cover five years; G.17/G.19 use full official
release reconciliation; BEA and Census retain family-specific annual/deep rules.

BLS, Census, and DOL comparisons are exact with zero tolerance. BEA and Federal Reserve
retain Live-validated source-rounding policies. A future precision change fails and requires
review instead of silently widening tolerance.

## Warning lifecycle

- JOLTS optional catalog and BEA reference-year omissions are mapping metadata warnings.
- Provider lag is a runtime operational warning.
- Old credential/business failures are immutable historical audit warnings; later provider
  success makes them superseded for current health without deleting them.
- Missing markers are data phenomena unless the expected latest period is affected.

No current values, DOL lag counts, request counts, or runtime snapshots are versioned here.

## Date-only PIT, readiness, and Stage 3 contract

`as_of` accepts `date` only and means the information set available at end of that
calendar date. It may read ALFRED rows with `vintage_date <= as_of`. It does not mean
start of day, market open, 08:30 ET, or another intraday cutoff. Timestamp precision
raises `PIT_GRANULARITY_UNSUPPORTED` rather than silently truncating to a date.

All 33 FRED reference identities passed the Stage 2B-5 limited ALFRED coverage gate at
an explicit per-series evidence date. Their vintage policy is
`PIT_SAFE_VIA_ALFRED_PROXY` with `PIT_SAFE_DATE_EOD`. ALFRED remains vintage only,
never a current fallback, and current official snapshots are never presented as PIT
history. A future backtest must retain both the official current primary identity and
the separate FRED Series ID used by the ALFRED proxy.

The future canonical output includes indicator/date/value/unit/frequency, source identity,
policy and mapping versions, fallback reason, mapping quality, provider and indicator
health, release/retrieval dates, vintage source/date, and PIT safety. Stage 3 must not read
provider raw tables, normalize, choose a source, alter frequency, or reinterpret seasonality.

Freeze PASS requires 33/33 complete contracts, zero configuration/materialization drift,
one eligible primary, complete resolution policies, zero current unresolved source,
normalization, or seasonal conflicts, and zero current error or critical events. A runtime
`DEGRADED` state is nonblocking only within grace with explicit fallback policy.

## DLCR-001 amendment: arbitrary-date PIT materialization

DLCR-001 extends the frozen date-EOD ALFRED contract without changing registry policy,
source roles, canonical units, frequency, seasonality, or the registry hash. A canonical
PIT request may now use any calendar `date`, including a weekend or a date absent from
`validated_as_of_dates`. Those dates remain live-gate evidence rather than an allowlist.

Resolution first requires `PIT_SAFE_VIA_ALFRED_PROXY`, `PIT_SAFE_DATE_EOD`, and
`END_OF_DAY`. It then reads an exact local `indicator_id + vintage_as_of` snapshot. On a
miss, the source-registry on-demand entry reuses the existing FRED client, credential-free
cache key, raw archive, request audit, and `observation_vintages` persistence before
re-running canonical resolution. No new table or migration is introduced.

Each materialized snapshot uses `realtime_start = realtime_end = vintage_as_of`, excludes
observation dates after that date, and is isolated from current observations and other
as-of dates. Empty, malformed, unavailable, unsafe-capability, or failed ALFRED results
fail closed; current FRED or official-source snapshots are never substituted. Timestamp
inputs remain unsupported and are never truncated.

## DLCR-002 amendment: current and PIT health separation

DLCR-002 adds operational classification without changing the frozen registry, canonical
data contract, provider identities, or database schema. `CURRENT_RUNTIME` and
`PIT_AVAILABILITY` are orthogonal quality scopes. Audit retention, resolution, and
supersession remain lifecycle properties rather than a third scope.

FRED continues to be stored as provider `fred`, while request evidence is evaluated in
separate `FRED_CURRENT` and `ALFRED_VINTAGE` runtime domains. A PIT request cannot degrade
current FRED health, activate current fallback, change the primary source, create current
freshness findings, or block `CURRENT_DATA_READY` without independent current-domain
evidence.

PIT readiness now separates the frozen contract, ALFRED runtime health, and the availability
of one `indicator_id + as_of` information set. Expected date-level outcomes such as
`OUTSIDE_COVERAGE`, `NO_VINTAGE`, and a legitimate `EMPTY_INFORMATION_SET` fail that
request closed but do not represent an ALFRED outage. Transport, authentication, rate-limit,
server, malformed-response, unsafe, and unknown failures remain typed and fail closed.
Ambiguous HTTP 400 responses are never treated as coverage evidence by themselves.

New PIT audit payloads and request evidence carry typed scope, runtime domain, availability
status, safe error category, optional HTTP status, blocking semantics, and classification
version. Their fingerprint includes scope, domain, indicator, as-of date, status, and safe
category. Existing audit rows remain immutable. Legacy rows are classified as PIT only from
reliable run operation, run parameters, or exact realtime request evidence; ambiguous legacy
records remain conservatively visible to current-health review.
