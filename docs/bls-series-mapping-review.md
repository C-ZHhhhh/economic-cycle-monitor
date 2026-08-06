# BLS Series Mapping Review

These 14 mappings were reviewed against BLS official Series Report pages. They are exact
configuration values: runtime validation may mark a mapping invalid or unavailable, but
the program never guesses, substitutes, or changes a Series ID.

| Indicator | FRED | BLS Series ID | BLS scope | Unit | Freq. | Adj. | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| US_CPI | CPIAUCSL | CUSR0000SA0 | CPI-U all items, U.S. city average | 1982-84=100 index | M | SA | valid |
| US_CORE_CPI | CPILFESL | CUSR0000SA0L1E | CPI-U less food and energy | 1982-84=100 index | M | SA | valid |
| US_PPI_COMMODITIES | PPIACO | WPU00000000 | PPI all commodities | 1982=100 index | M | NSA | valid |
| US_NONFARM_EMPLOYMENT | PAYEMS | CES0000000001 | Total nonfarm all employees | thousands persons | M | SA | valid |
| US_UNEMPLOYMENT_RATE | UNRATE | LNS14000000 | Civilian unemployment rate | percent | M | SA | valid |
| US_U6_RATE | U6RATE | LNS13327709 | U-6 labor underutilization | percent | M | SA | valid |
| US_PARTICIPATION_RATE | CIVPART | LNS11300000 | Labor-force participation | percent | M | SA | valid |
| US_EMPLOYMENT_POP_RATIO | EMRATIO | LNS12300000 | Employment-population ratio | percent | M | SA | valid |
| US_JOB_OPENINGS | JTSJOL | JTS000000000000000JOL | Total nonfarm openings level | thousands persons | M | SA | valid |
| US_HIRES | JTSHIR | JTS000000000000000HIR | Total nonfarm hires rate | rate | M | SA | valid |
| US_QUITS | JTSQUR | JTS000000000000000QUR | Total nonfarm quits rate | rate | M | SA | valid |
| US_TEMP_HELP_EMPLOYMENT | TEMPHELPS | CES6056132001 | Temporary help services employees | thousands persons | M | SA | valid |
| US_MANUFACTURING_HOURS | AWHMAN | CES3000000007 | Production/nonsupervisory manufacturing weekly hours | hours | M | SA | valid |
| US_AVERAGE_HOURLY_EARNINGS | AHETPI | CES0500000008 | Production/nonsupervisory total-private earnings | dollars/hour | M | SA | valid |

Each `validation_url` is versioned with the mapping in `config/data_sources/bls.yaml` and
points to `https://data.bls.gov/timeseries/{BLS_SERIES_ID}`. The reviewed distinctions are
intentional: JOL uses a level, HIR and QUR use rates, PPI is not seasonally adjusted, and
the CES hours/earnings mappings preserve their production-and-nonsupervisory coverage.

If BLS omits a requested ID, the mapping becomes `unavailable`. If returned catalog
semantics conflict, it becomes `invalid`. Lack of optional catalog fields is a warning;
it is not permission to replace the Series ID.
