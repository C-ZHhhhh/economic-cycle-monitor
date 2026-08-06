# Official Source Cross-Validation

BLS is primary, FRED is fallback, and ALFRED remains the on-demand historical vintage
source for the 14 mapped indicators. Comparison never overwrites either source and never
chooses a value automatically.

Before numeric comparison, the service requires the configured unit and seasonal
adjustment to match the indicator semantics. It currently supports the reviewed `identity`
normalization rule only. Dates are read in bounded pages; no DataFrame or full-history
in-memory join is used.

| Status | Meaning | Typical quality severity |
| --- | --- | --- |
| `match` | Same aligned value | none |
| `match_with_rounding` | Absolute difference is within that indicator's YAML tolerance | none |
| `distribution_lag` | Fresh BLS point is not yet present in FRED within its lag allowance | info |
| `missing_in_bls` | FRED has an older point absent from the primary source | warning |
| `missing_in_fred` | BLS point is absent from FRED beyond the lag allowance | warning |
| `normalization_mismatch` | Units cannot be safely aligned | error |
| `seasonal_adjustment_mismatch` | Adjustment scopes differ | error |
| `source_value_conflict` | Comparable values exceed configured absolute tolerance | error |
| `not_comparable` | Neither value exists or the normalization rule is unsupported | none/error |

Tolerance, distribution lag, revision lookback, and normalization rule are per mapping in
`config/data_sources/bls.yaml`. A value conflict is excluded from automatic downstream
selection; Stage 2B-1 has no scoring path in which it could participate.
