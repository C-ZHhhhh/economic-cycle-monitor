# DLCR-001: Arbitrary-Date ALFRED PIT Snapshots

## Decision

The canonical Stage 2 resolver supports point-in-time reads for an arbitrary calendar
date at end-of-day granularity. Stage 3 consumers call
`get_effective_observations_on_demand(...)`; they do not call ALFRED or provider-specific
tables directly.

The frozen registry remains `stage2b5-v3`. `validated_as_of_dates` records completed live
evidence and is not a runtime allowlist. A request is eligible only when its indicator
policy declares all of:

- `PIT_SAFE_VIA_ALFRED_PROXY`
- `PIT_SAFE_DATE_EOD`
- `END_OF_DAY`

## Resolution contract

1. Validate a date-only request, indicator materialization, and PIT capability.
2. Read the exact local `indicator_id + vintage_as_of` ALFRED snapshot.
3. If absent, issue one existing-client request with equal `realtime_start` and
   `realtime_end` values.
4. Reuse the existing secret-free file cache, raw archive, request audit, sync audit, and
   `observation_vintages` storage.
5. Exclude observations dated after `vintage_as_of`, persist the complete returned
   information set, and resolve the requested range canonically.
6. Return `EffectiveObservation` values with canonical unit and frequency, FRED reference
   identity, ALFRED vintage identity, retrieval time, `vintage_as_of`, registry version and
   hash, and `is_point_in_time_safe = true`.

Repeated reads of the same indicator/date use the local exact snapshot and do not issue a
new provider request. Different dates remain separate snapshots. Current resolution and
current official-source selection are unchanged.

## Failure and security contract

The resolver fails closed for unsupported timestamp precision, unsafe or unavailable PIT
capability, missing credentials, provider errors, malformed responses, and empty
information sets. It never returns a current snapshot as historical PIT data.

Credentials are excluded from cache keys, request audit, errors, logs, raw archive paths,
and canonical output. Raw responses, caches, local reports, and `.env` remain ignored and
must not be committed.

## Storage and migration decision

No migration is required. The existing `observation_vintages` model unambiguously stores
exact requested snapshots using `release_stage = as_known_on`, `source = fred`, and the
requested date in both `vintage_date` and `source_vintage_date`. Existing sync and request
audit tables record materialization outcomes without adding a second PIT client or a bulk
vintage warehouse.
