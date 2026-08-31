# DLCR-003 PIT information-set semantic reuse

DLCR-003 changes physical PIT materialization, not economic definitions. Its authority is
`config/data_sources/pit_materialization.yaml` (`pit-materialization-v2`) and is bound to the
unchanged `stage2b5-v3` source registry.

## Resolver contract

For one indicator and closed date-EOD `evaluation_as_of`, the resolver:

1. validates source-registry and PIT-materialization authorities;
2. selects a complete immutable change-index generation covering the EOD;
3. chooses `max(change_date <= evaluation_as_of)` by binary search;
4. verifies and reuses the corresponding immutable semantic information set;
5. materializes only that source change date when it is absent; and
6. returns both evaluation and source-information dates plus all authority and result hashes.

An EOD beyond `index_complete_through` triggers a sequential incremental refresh that creates a
new generation. Failure does not mutate or extend the old generation and the PIT request fails
closed. Historical reconciliation uses exact changed-date intervals so unrelated history is not
invalidated.

## Identity and correction lineage

The semantic hash covers indicator/provider identity, official series contract, source change
date, frozen registry and materialization authorities, canonical unit/frequency, sorted
observation dates, canonical Decimal values, and explicit missing statuses. It excludes runtime,
retrieval, request, retry, and storage-location fields.

The evaluation hash separately covers requested EOD, semantic hash, requested range, and the
`pit-calculation-provenance-v2` resolver contract. Two evaluation EODs can therefore share one
semantic hash while retaining distinct evaluation hashes.

A provider correction for the same source change date creates a new information-set hash and
supersession link. Old headers and observations remain immutable. A different raw payload with
the same canonical semantics is idempotent.

## Compatibility and safety

- `ObservationVintage.vintage_date` is unchanged.
- `vintage_as_of` remains a deprecated alias for requested `evaluation_as_of`.
- legacy exact snapshots and their audit lineage are preserved.
- the exact resolver is callable only by the explicit offline equivalence comparator.
- current FRED/official-source resolution never reads PIT index or information-set tables.
- PIT failures remain scoped to `PIT_AVAILABILITY` and `ALFRED_VINTAGE`.
- credentials, keyed URLs, headers, raw payloads, cache paths, and reports are not persisted by
  the new identity layer.

## First Pilot equivalence and request estimate

The approved offline gate compares all 594 First Pilot indicator/EOD snapshot pairs and the nine
production replay EODs. The local evidence grouped those 594 snapshots into 131 distinct
consecutive semantic information sets, yielding 463 reusable evaluations. A one-page-per-series
theoretical workload is 33 change-index requests plus 131 information-set requests, or 164
requests instead of 594 exact snapshot requests (about a 72% reduction). Actual pagination and
future source changes remain governed by the hard request budget and concurrency of one.

Stage 3F-v1 and its historical artifacts remain immutable. Stage 3F-v2 only binds the new PIT
materialization and calculation-provenance authorities and authorizes offline equivalence; the
Second Pilot and full historical run remain unapproved.
