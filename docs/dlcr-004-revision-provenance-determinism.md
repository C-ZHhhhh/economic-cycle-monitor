# DLCR-004 deterministic revision provenance

DLCR-004 replaces process-local revision inference with the independent
`revision-provenance-v2` authority. It does not change the economic formulas or versions of
Stage 3A through Stage 3E, the `stage2b5-v3` source registry, or
`pit-materialization-v2`.

## Canonical predecessor

For a persisted semantic information set, the only canonical predecessor is:

1. an explicitly linked superseded information set for the same source-information date; or
2. the immediate prior change date in the same immutable, authoritative change-index
   generation.

Row order, database IDs, timestamps, calculation order, caches, and prior in-memory results are
never comparison authority. When neither relationship exists, evidence is typed
`NO_COMPARABLE_PREDECESSOR` and no revision is inferred.

## Observation-set envelope

The comparator verifies both immutable information sets and compares their canonical states in
observation-date order. The envelope records current and predecessor identities and counts,
added, removed, revised, and status-changed dates, typed deltas, a canonical semantic-diff hash,
and a revision-evidence hash.

The typed vocabulary is `NEW_INFORMATION`, `SOURCE_REVISION`, `OBSERVATION_REMOVAL`,
`OBSERVATION_STATUS_CHANGE`, `DISTRIBUTION_CHANGE`, and `RESULT_CHANGE`. A newly added date is
normal information progression and never becomes a revision flag by itself.

## Stage 3 propagation

- Stage 3A evaluates the configured feature both with the current canonical inputs and with the
  persisted predecessor values for affected inputs.
- Stage 3B independently compares the normalized result and its prior-only reference
  distribution under that counterfactual.
- Stage 3C derives the pillar effect from constituent signal effects, without remembering an
  earlier pillar call.
- Stage 3D and Stage 3E propagate deterministic upstream evidence and add `RESULT_CHANGE` only
  when their own complete typed result changed.

Calculation hashes bind the provenance authority and deterministic effect hash only for PIT
calculations. The current resolver and current calculation-hash path remain isolated. Bounded
process-local caches may memoize pure persisted comparisons, but cache state cannot alter any
result, flag, or hash.

## Validation contract

Stage 3F-v1 artifacts remain immutable. Stage 3F-v2 additionally binds
`revision-provenance-v2`. Its merge gate is a zero-network focused replay around 2013-12-05,
followed by a fresh 1,966-EOD v2 baseline, zero economic-semantic differences from the completed
v1 history, and an independent exact v2 replay. Stage 3F remains `FAIL_NONDETERMINISM` until all
of those gates pass.

No database model or migration is introduced by DLCR-004.
