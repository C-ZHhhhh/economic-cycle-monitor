# Stage 2B-2 BEA Official-Source Adapter

Stage 2B-2 adds only the BEA NIPA adapter. It does not add Census, Federal Reserve,
DOL, scoring, feature calculation, scheduling, Stage 3 logic, or live LLM calls.

## Mapping gate

The five mappings in `config/data_sources/bea.yaml` began as `pending_review` candidates.
An explicitly authorized live GetData run and full BEA/FRED comparison confirmed all five;
the two price-index mappings are `valid`, while the three chained-dollar mappings are
`valid_with_warning` because row-level unit text omits the trusted table reference year.
A configured mapping never bypasses validation. Every response must match
the exact dataset, TableName, SeriesCode, LineNumber, frequency, unit semantics, and
economic meaning before any row can be written. A mismatch stops only that indicator and
is never used to substitute another line or series automatically.

| Indicator | TableName | SeriesCode | LineNumber | Frequency | Reviewed status |
| --- | --- | --- | --- | --- | --- |
| US_REAL_GDP | T10106 | A191RX | 1 | Q | valid_with_warning |
| US_REAL_PCE | T20806 | DPCERX | 1 | M | valid_with_warning |
| US_REAL_DISPOSABLE_INCOME | T20600 | A067RX | 37 | M | valid_with_warning |
| US_PCE_PRICE | T20804 | DPCERG | 1 | M | valid |
| US_CORE_PCE_PRICE | T20804 | DPCCRG | 25 | M | valid |

T20804 is fetched once per year batch. Its headline and core rows are validated and
persisted independently. No ingestion code interpolates, repeats, downsamples, or otherwise
changes source frequency.

The live BEA GetData response confirmed DPCCRG on line 25 with the description
`PCE excluding food and energy`. For T20804 price-index rows, BEA's normal shape is
`Metric_Name=Fisher Price Index`, `CL_UNIT=Level`, and `UNIT_MULT=0`. The 2017=100
reference basis is trusted only after the exact table, series, and line identity gate;
row-level unit text is not required to repeat the reference year.

Official NIPA parameter metadata supplies each table's first available year. Requests
starting before that bound omit unavailable years instead of inventing observations or
turning a normal source-coverage boundary into a failed indicator. In particular,
T20806 monthly data begins in 2007.

## Data and audit behavior

The client supports GetDatasetList, GetParameterList, GetParameterValues, and GetData. It
uses explicit years, one table per request, bounded five-year batches, timeouts, retryable
429/5xx handling, exponential backoff, sanitized caching, and raw response archives. HTTP
200 with a BEA business error is a failed request.

BEA `UNIT_MULT` is treated as a base-10 exponent. `DataValue`, `CL_UNIT`, `UNIT_MULT`, and
the normalized canonical value are all retained in `source_details_json`. Missing values
remain missing and are never converted to zero. BEA observations use `source=bea`, so they
cannot overwrite FRED or BLS rows. Same-source changes append an
`observation_vintages` revision before updating the current BEA value.

The API key exists only in local runtime settings. Any response echo of `UserID` is removed
before cache, archive, request audit, log, exception, or API output. Default tests use only
mock HTTP transports.

## Manual live gate

Do not run these commands until the user explicitly authorizes the BEA Live Smoke Test.
The first gate is only real GDP:

```bash
python -m app.cli validate-bea-mappings --indicator US_REAL_GDP --json
python -m app.cli backfill-bea --indicator US_REAL_GDP --start-year 2024 --json
python -m app.cli compare-sources --primary bea --secondary fred \
  --indicator US_REAL_GDP --json
```

Only after exact GDP metadata, recent data, idempotency, and comparison pass should the
2000-to-present GDP backfill and then all five candidates be attempted. Smoke artifacts,
raw responses, cache files, `.env`, and generated reports remain local.

## Migration

`0004_stage2b_bea` adds only `source_parameters_json` to the existing source-neutral
mapping table. This stores table-oriented request identity without a BEA-specific database
structure. It depends on `0003_stage2b_official_sources`; no historical migration is
modified. Downgrade removes only the new column.
