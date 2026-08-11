# Stage 2B-3 Census candidate mappings

The explicit Census Live Gate completed on 2026-08-11. All seven identities in
this document and `config/data_sources/census.yaml` are now reviewed `valid`
mappings. The internal `source_series_id` values are stable repository keys, not
Census-issued series identifiers. Census identity is the complete set of
official dimensions stored in `source_parameters_json` and observation audit
details.

| Indicator | Dataset | Category | Data type | Seasonal | Geography | Unit/scale | Role | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| US_REAL_RETAIL_SALES | marts | 44X72 | SM | yes | US | MLN$ / 1,000,000 | SUPPORTING_COMPONENT | valid |
| US_DURABLE_GOODS_ORDERS | m3 | MDM | NO | yes | US | MLN$ / 1,000,000 | DIRECT | valid |
| US_CORE_CAPITAL_GOODS_ORDERS | m3 | NXA | NO | yes | US | MLN$ / 1,000,000 | DIRECT | valid |
| US_HOUSING_STARTS | resconst | ASTARTS | TOTAL | yes | US | K / 1,000 | DIRECT | valid |
| US_BUILDING_PERMITS | resconst | APERMITS | TOTAL | yes | US | K / 1,000 | DIRECT | valid |
| US_BUSINESS_INVENTORIES | mtis | TOTBUS | IM | yes | US | MLN$ / 1,000,000 | DIRECT | valid |
| US_INVENTORY_SALES_RATIO | mtis | TOTBUS | IR | yes | US | ratio / 1 | DIRECT | valid |

Live responses confirmed `seasonally_adj=yes`, `geo_level_code=US`, monthly
frequency, U.S. national geography, the documented units and scales, and exact
agreement with the corresponding FRED observations from 2000-01 through each
dataset's latest valid month. `program_code` is included in the source identity
hash. M3 uses one-year request windows to keep response size bounded; the other
datasets use five-year windows.

## Real retail sales boundary

Census MARTS publishes nominal Advance Retail and Food Services Sales. FRED
RRSFS is a derived real series built from nominal RSAFS and CPIAUCSL. The MARTS
mapping therefore may be metadata-validated only. It must not write Census
observations for `US_REAL_RETAIL_SALES`, become its primary source, enter the six
DIRECT counters, or compare nominal Census values with FRED RRSFS. No derivation
recipe is implemented in Stage 2B-3.

## Live Gate evidence

The gate began with `US_HOUSING_STARTS` and then independently validated the
other five DIRECT identities. The final stored Census/FRED comparison covered
1,906 observations: 1,906 exact matches, zero rounding matches, zero missing
dates, zero source conflicts, and zero normalization or seasonal-adjustment
mismatches. Repeating the complete backfill inserted zero observations. No code
or adjacent category was substituted automatically.
