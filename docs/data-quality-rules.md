# Stage 2 Data Quality Rules

| Rule | Default severity | Meaning |
| --- | --- | --- |
| `missing_value` | warning | FRED returned `.`; it is never replaced by zero. |
| `duplicate_observation` | error | One response repeats an observation date. |
| `invalid_numeric_value` | error | A response or stored value is non-finite. |
| `observation_date_in_future` | error | An observation date is later than the check date. |
| `metadata_frequency_mismatch` | error | Source and configured frequencies conflict. |
| `metadata_unit_mismatch` | warning | Source unit semantics require review. |
| `metadata_seasonal_adjustment_mismatch` | error | Seasonal-adjustment semantics conflict. |
| `stale_series` | warning | Latest value exceeds the configured staleness threshold. |
| `no_recent_observations` | error | No current observation exists. |
| `unexpected_large_revision` | warning | Revision exceeds 50% of the prior absolute value. |
| `non_monotonic_dates` | error | Response dates move backward. |
| `empty_api_response` | error | A successful response contains no observations. |
| `source_unavailable` | error | FRED fails after retries or is unavailable. |
| `raw_response_hash_conflict` | critical | One archive path has conflicting hashes. |
| `duplicate_vintage` | error | A logical indicator/date/vintage is duplicated. |

Findings use stable fingerprints and are not duplicated while unresolved. Resolution is
explicit through `resolved_at` and `resolution_notes`; checks do not silently erase prior
events. These rules validate data integrity only—none computes economic scores or regimes.
