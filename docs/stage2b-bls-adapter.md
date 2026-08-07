# Stage 2B-1 BLS Official-Source Adapter

Stage 2B-1 adds BLS only. It does not add BEA, Census, Federal Reserve, DOL, scoring,
features, scheduling, Stage 3 logic, or live LLM calls.

## Data flow

1. `seed-indicators` synchronizes the 33-indicator catalog and the 14 reviewed BLS
   mappings.
2. `validate-bls-mappings` submits one BLS v2 batch with `catalog=true`, requires exact
   returned Series IDs, and records missing or conflicting metadata without changing IDs.
3. `backfill-bls` splits inclusive history by the configured registered/unregistered BLS
   year limit and requests all selected series in each window.
4. `sync-bls` groups series by their configured revision lookback and keeps same-source
   changes in `observation_vintages`.
5. `compare-sources` reads BLS and FRED in bounded date pages and stores auditable results.

`raw_observations` remains unique by indicator, observation date, and source. BLS writes
`source=bls`; the existing `source=fred` row is never overwritten or deleted. BLS
footnotes, period codes, preliminary flags, and source Series IDs are retained in
`source_details_json`.

## Client limits and security

The client uses `httpx.AsyncClient` and POST `/timeseries/data/`. Registered mode uses a
maximum of 50 series and 20 inclusive years per request; public mode uses 25 series and
10 years. These limits, timeouts, retries, and cache TTL are settings rather than service
constants. Optional catalog, annual-average, calculations, and aspects flags default off.

The registration key is added only to the in-memory transport body. It is excluded from
cache keys, raw archives, request audits, errors, CLI/API output, and filenames. Automated
tests inject `httpx.MockTransport`; none calls BLS or another external API.

## Manual live gate

After putting any key only in local `.env`, set `BLS_ENABLED=true` and test one series:

```bash
python -m app.cli validate-bls-mappings --indicator US_CPI --json
python -m app.cli backfill-bls --indicator US_CPI --start 2024 --json
python -m app.cli compare-sources --indicator US_CPI --json
```

Review the Series ID, mapping status, request audit, raw archive, cache, and comparison
classifications before running all 14 mappings. Live output and generated reports remain
local and must not be committed.

## Migration

`0003_stage2b_official_sources` adds versioned `indicator_source_mappings`, paged/auditable
`source_comparison_results`, and JSON source details on current/vintage observations. It
depends on `0002` and does not edit either historical migration. Downgrade removes only
Stage 2B structures.
