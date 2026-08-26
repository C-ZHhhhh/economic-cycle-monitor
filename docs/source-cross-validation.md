# Official Source Cross-Validation

The unified source registry defines primary, FRED current fallback, and ALFRED vintage
roles across BLS, BEA, Census, Federal Reserve Board, and DOL. Comparison answers only
whether independently stored sources agree. It never overwrites either source and never
chooses a value for the resolver.

Before numeric comparison, each provider adapter requires its reviewed identity, unit,
scale, frequency, seasonal adjustment, and normalization evidence. Dates are processed in
bounded pages without a full-history in-memory DataFrame join.

| Status | Meaning | Typical severity |
| --- | --- | --- |
| `match` | Same aligned canonical value | none |
| `match_with_rounding` | Difference is within registry-approved source rounding | none |
| `distribution_lag` | Official point is not yet in FRED within its lag allowance | info |
| `missing_in_primary_source` | FRED has an older point absent from official primary | warning |
| `missing_in_fred` | Official point is absent from FRED beyond distribution allowance | warning |
| `normalization_mismatch` | Units or scale cannot be safely aligned | error |
| `seasonal_adjustment_mismatch` | Seasonal scopes differ | error |
| `source_value_conflict` | Comparable values exceed registry tolerance | error |
| `not_comparable` | Identity or normalization evidence is incomplete | none/error |

Tolerance and distribution-lag policy are owned only by
`config/data_sources/source_registry.yaml`. Provider YAML owns transport identity and
normalization evidence. BLS, Census, and DOL are exact with zero tolerance; BEA and
Federal Reserve retain Live-validated source rounding. Any value, identity, normalization,
or seasonal conflict fails resolution closed and cannot activate fallback.
