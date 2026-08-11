# Stage 2B-3 Census candidate mappings

All mappings in this document and `config/data_sources/census.yaml` remain
`pending_review` until the explicit Census Live Gate verifies V1-V10. The
internal `source_series_id` values are stable repository keys, not Census-issued
series identifiers. Census identity is the complete set of official dimensions
stored in `source_parameters_json` and observation audit details.

| Indicator | Dataset | Category | Data type | Role |
| --- | --- | --- | --- | --- |
| US_REAL_RETAIL_SALES | marts | 44X72 | SM | SUPPORTING_COMPONENT |
| US_DURABLE_GOODS_ORDERS | m3 | MDM | NO | DIRECT |
| US_CORE_CAPITAL_GOODS_ORDERS | m3 | NXA | NO | DIRECT |
| US_HOUSING_STARTS | resconst | ASTARTS | TOTAL | DIRECT |
| US_BUILDING_PERMITS | resconst | APERMITS | TOTAL | DIRECT |
| US_BUSINESS_INVENTORIES | mtis | TOTBUS | IM | DIRECT |
| US_INVENTORY_SALES_RATIO | mtis | TOTBUS | IR | DIRECT |

The candidate semantics require seasonally adjusted U.S. national data. The
exact `seasonally_adj` and `geo_level_code` wire values are intentionally null
until a real response is reviewed. A pending mapping is not eligible for
backfill, synchronization, primary-source selection, or source comparison.

## Real retail sales boundary

Census MARTS publishes nominal Advance Retail and Food Services Sales. FRED
RRSFS is a derived real series built from nominal RSAFS and CPIAUCSL. The MARTS
mapping therefore may be metadata-validated only. It must not write Census
observations for `US_REAL_RETAIL_SALES`, become its primary source, enter the six
DIRECT counters, or compare nominal Census values with FRED RRSFS. No derivation
recipe is implemented in Stage 2B-3.

## Live Gate

The first authorized live test should use `US_HOUSING_STARTS`. Live validation
must confirm dataset, endpoint, category, data type, monthly frequency,
seasonality, unit and scale, economic meaning, U.S. geography, historical
coverage, and a small Census/FRED value comparison before any mapping status is
changed. Codes are never substituted automatically.
