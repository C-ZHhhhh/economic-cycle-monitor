# Stage 2 FRED Data Pipeline

## Scope and flow

Stage 2 registers 33 US macro indicators and builds an auditable FRED ingestion pipeline.
It stops before feature engineering, scoring, cycle classification, Nowcast, DeepSeek,
additional agents, or a frontend.

1. `seed-indicators` synchronizes the YAML catalog without deleting history.
2. `validate-fred-metadata` stores Series/Release metadata and assigns `valid`,
   `valid_with_warning`, `invalid`, or `unavailable`.
3. `backfill-fred` loads a bounded history only for validated indicators.
4. `sync-fred` uses frequency-specific lookbacks to capture additions and revisions.
5. `fetch-fred-vintage` requests one ALFRED as-known-on date; no bulk crawl occurs.
6. Every operation writes `sync_runs`; provider calls write sanitized
   `data_source_requests`; raw bytes are hash-addressed gzip files under `data/raw/fred`.
7. Quality findings are deduplicated while unresolved in `data_quality_events`.

`raw_observations` contains the latest known value per indicator/date/source. When a value
changes, its prior/current pair and revision number are appended to `observation_vintages`
before the current row changes. Explicit ALFRED snapshots also use the vintage table and
never replace current observations.

## Security, reliability, and resources

- The FRED key is loaded only at runtime and excluded from cache keys, audit parameters,
  errors, API responses, filenames, and configuration summaries.
- `data/raw/`, `data/cache/`, and generated `outputs/*` are ignored by Git.
- Default pytest selection excludes `live`; automated HTTP tests use MockTransport.
- Calls are asynchronous, bounded by global/per-source semaphores, and retry timeouts,
  429, and selected 5xx responses with exponential backoff and `Retry-After` support.
- API pages have hard limits. No pandas, multiprocessing, Redis, or task queue is used.

## Migration behavior

`0002_stage2_fred_pipeline` is incremental; `0001_initial_schema` is unchanged. Tests
exercise upgrade, downgrade to Stage 1, and re-upgrade. Downgrade removes Stage 2 metadata,
request audits, sync runs, vintage extensions, and quality data, so use it only on a
disposable database.

## Manual live smoke test

Live access was not used during implementation. After putting the key only in local `.env`:

```bash
python -m app.cli seed-indicators --json
python -m app.cli validate-fred-metadata --indicator US_CPI --json
python -m app.cli backfill-fred --indicator US_CPI --start 2024-01-01 --json
python -m app.cli check-data-quality --indicator US_CPI --json
```

After reviewing the one-Series result, run the full metadata validation and backfill.

| Metric | Result |
| --- | --- |
| Start/end UTC | Pending manual live run |
| Peak resident memory | Pending |
| Total runtime | Pending |
| API request count | Pending |
| Cache hit count | Pending |
| Current observation rows | Pending |
| Vintage rows | Pending |
| Failed indicators | Pending |
| Quality events | Pending |

Use `show-sync-status --json` and PostgreSQL counts to complete this record. Never paste
the key or `.env` into it.
