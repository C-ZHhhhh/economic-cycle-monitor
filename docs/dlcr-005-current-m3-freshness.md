# DLCR-005: Census Full M3 Current Freshness Authority

## Decision

DLCR-005 introduces `current-freshness-v1` as an independent, current-mode-only
authority. Its sole configuration authority is
`config/data_sources/current_freshness.yaml`. It is bound to Source Registry
`stage2b5-v3` and registry hash
`7a13095907733a1d6092ef5231c67de7528886c7eda9eeb73ed2166764834758`, but it
does not modify or supersede that registry.

The authority applies only to the Census Full Manufacturers' Shipments,
Inventories, and Orders (Full M3) release family and exactly these indicators:

- `US_DURABLE_GOODS_ORDERS`
- `US_CORE_CAPITAL_GOODS_ORDERS`

Membership is explicit. It is never inferred from provider, dataset, frequency,
or nearby Census programs. Advance M3, housing, permits, and MTIS release times
cannot activate a Full M3 observation period.

## Reason

The prior generic Census current-freshness contract used an approximate 45-day
lag plus a 15-day grace window. For the July 2026 Full M3 period, the official
release was September 2, 2026 at 10:00 America/New_York. The approximation could
therefore report stale data before the official Full Report existed. This was a
release-calendar error, not an ingestion failure.

`current-freshness-v1` uses reviewed Census release entries. A period becomes
expected only at its effective Full M3 release timestamp. A missing primary is
`DEGRADED` from that instant until, but not including, 48 elapsed hours and is
`STALE` at or after the 48-hour boundary. The duration is absolute: weekends,
holidays, and business-day rules do not extend it.

## Calendar states and provenance

- `SCHEDULED` activates at the reviewed scheduled timestamp.
- `RESCHEDULED` activates only at the replacement timestamp and retains the
  prior timestamp, reason, and official evidence.
- `RELEASED` may use an actual release timestamp; a later manual verification
  timestamp never delays activation.
- `TBD` and `SUSPENDED` never receive synthetic dates.

When the next explicit period is `TBD` or `SUSPENDED`, the last period with a
legal effective release remains expected and the runtime emits a calendar
diagnostic instead of inventing a data-staleness finding. If the next required
period entry is absent, current readiness fails closed with
`CURRENT_CALENDAR_COVERAGE_EXPIRED`. A low future horizon produces the separate,
non-blocking `CURRENT_FRESHNESS_CALENDAR_LOW_HORIZON` maintenance warning. Three
future period entries may include `TBD`; three confirmed timestamps are not
required.

## Runtime audit metadata

Current health snapshots carry the current freshness version/hash, release
family, active calendar-entry identity, and effective release timestamp.
Readiness carries the current freshness version/hash. These fields are runtime
audit metadata only.

The current resolver remains a pure persisted-data read. Primary-stale readiness
continues to block even when a fresh fallback exists, and fallback authority is
unchanged.

## Historical isolation

DLCR-005 does not change:

- `source_registry.yaml`, `stage2b5-v3`, or its registry hash;
- ALFRED, PIT information-set, pit-materialization-v2, or
  revision-provenance-v2 contracts;
- Stage 3A through Stage 3F authorities or economic formulas;
- the approved Stage 3F history hash
  `0bb815c36c8c9171e326767cc737935e492397e2b7dd5deda55958f287ae7db1`.

The authority is intentionally non-retroactive before its first reviewed
calendar entry. No database storage or migration is introduced. Calendar
maintenance occurs only through a reviewed configuration change based on the
official Census M3 release schedule, the Census Economic Indicator Calendar,
and official release confirmation pages. Runtime code does not fetch calendars.
