# DLCR-002: PIT Health Separation

## Decision

DLCR-002 separates current production health from point-in-time availability while preserving
the Stage 2 source-of-truth registry and the DLCR-001 date-EOD resolver contract. It introduces
no migration and does not rename the FRED provider.

## Operational model

The model has three independent questions:

1. `CURRENT_RUNTIME`: can the current production interface provide canonical current data?
2. `ALFRED_VINTAGE` runtime health: is the vintage interface operational?
3. PIT request availability: can one `indicator_id + as_of` date-EOD information set be
   resolved safely?

FRED request evidence is tagged as `FRED_CURRENT` or `ALFRED_VINTAGE`. Current provider and
indicator health consume only current-domain evidence. ALFRED health consumes only vintage
domain evidence. A failure in either domain cannot be projected onto the other.

## PIT availability statuses

- `AVAILABLE`: a non-empty, valid date-EOD information set was safely materialized.
- `OUTSIDE_COVERAGE`: official vintage coverage proves the date predates coverage.
- `NO_VINTAGE`: official coverage evidence reports no vintage dates.
- `EMPTY_INFORMATION_SET`: a structurally valid response contains no value visible as of the
  requested date.
- `TEMPORARY_REQUEST_FAILURE`: timeout, network, rate-limit, or server failure.
- `AUTHENTICATION_FAILURE`: missing or rejected FRED credentials.
- `MALFORMED_RESPONSE`: a successful response violates the expected schema.
- `UNSAFE`: registry policy does not permit safe date-EOD PIT resolution.
- `UNKNOWN_FAILURE`: available evidence is insufficient for a narrower classification.

Every non-available outcome fails the individual PIT request closed. Expected coverage,
no-vintage, and legitimate empty-set outcomes are nonblocking for ALFRED runtime health.
Ambiguous HTTP 400 responses remain `UNKNOWN_FAILURE` unless a separate official vintage-date
request supplies sufficient coverage evidence.

## Readiness contract

`CURRENT_DATA_READY` consumes registry and materialization drift, current mapping/integrity,
current primary freshness, current provider health, and unresolved current-scoped error or
critical findings. PIT-scoped findings are excluded.

`PIT_CONTRACT_READY` means the registry permits PIT. `PIT_RUNTIME_READY` means the
`ALFRED_VINTAGE` domain is healthy. The compatibility field is:

`PIT_BACKTEST_READY = PIT_CONTRACT_READY AND PIT_RUNTIME_READY`

Per-request availability is not a global readiness input. One unsupported historical date
therefore cannot block all PIT backtesting.

## Audit and compatibility

Typed fields are stored in existing JSON audit columns and linked through existing sync runs.
No table or migration is added. New PIT event fingerprints include scope, runtime domain,
indicator, as-of date, availability status, and safe error category. Repeating the same failure
is idempotent; another date or status remains a separate event.

Historical audit rows are retained. Resolution and supersession can describe their lifecycle
without changing the original event evidence. Legacy PIT rows are recognized only by reliable
evidence, in this order:

1. explicit scope or runtime-domain metadata;
2. `SyncRun.operation == fetch_fred_vintage`;
3. run parameters include `vintage_date` or `as_of`;
4. request `realtime_start == realtime_end`.

Rows without reliable evidence remain conservative current/unknown findings for manual review.
No credentials, credential-bearing URLs, or unredacted response bodies are stored in these
classifications.
