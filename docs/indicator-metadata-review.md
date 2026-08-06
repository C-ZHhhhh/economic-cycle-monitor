# Indicator Metadata Review

No live FRED API call was authorized or made during implementation, so actual source
metadata is deliberately not fabricated. This table records every configured ID and the
expected semantics checked by `validate-fred-metadata`. Its generated
`outputs/metadata_validation_report.json` becomes the authoritative dated review.
Warnings never trigger an automatic Series ID substitution.

| Indicator | Series ID | Expected frequency/unit/adjustment | Initial review |
| --- | --- | --- | --- |
| US_REAL_GDP | GDPC1 | quarterly; chained dollars; SAAR | pending live validation |
| US_REAL_PCE | PCEC96 | monthly; chained dollars; SAAR | pending live validation |
| US_REAL_DISPOSABLE_INCOME | DSPIC96 | monthly; chained dollars; SAAR | pending live validation |
| US_INDUSTRIAL_PRODUCTION | INDPRO | monthly; index; SA | pending live validation |
| US_REAL_RETAIL_SALES | RRSFS | monthly; chained dollars; SA | semantic review required |
| US_NONFARM_EMPLOYMENT | PAYEMS | monthly; thousands persons; SA | pending live validation |
| US_CPI | CPIAUCSL | monthly; index; SA | pending live validation |
| US_CORE_CPI | CPILFESL | monthly; index; SA | pending live validation |
| US_PCE_PRICE | PCEPI | monthly; index; SA | pending live validation |
| US_CORE_PCE_PRICE | PCEPILFE | monthly; index; SA | pending live validation |
| US_PPI_COMMODITIES | PPIACO | monthly; index; NSA | semantic/seasonal review required |
| US_UNEMPLOYMENT_RATE | UNRATE | monthly; percent; SA | pending live validation |
| US_U6_RATE | U6RATE | monthly; percent; SA | pending live validation |
| US_PARTICIPATION_RATE | CIVPART | monthly; percent; SA | pending live validation |
| US_EMPLOYMENT_POP_RATIO | EMRATIO | monthly; percent; SA | pending live validation |
| US_INITIAL_CLAIMS | ICSA | weekly; number; SA | pending live validation |
| US_CONTINUING_CLAIMS | CCSA | weekly; number; SA | pending live validation |
| US_JOB_OPENINGS | JTSJOL | monthly; thousands persons; SA | pending live validation |
| US_HIRES | JTSHIR | monthly; thousands persons; SA | pending live validation |
| US_QUITS | JTSQUR | monthly; thousands persons; SA | pending live validation |
| US_TEMP_HELP_EMPLOYMENT | TEMPHELPS | monthly; thousands persons; SA | pending live validation |
| US_MANUFACTURING_HOURS | AWHMAN | monthly; hours; SA | coverage review required |
| US_AVERAGE_HOURLY_EARNINGS | AHETPI | monthly; dollars/hour; SA | worker-coverage review required |
| US_SAVING_RATE | PSAVERT | monthly; percent; SAAR | pending live validation |
| US_CONSUMER_CREDIT | TOTALSL | monthly; millions dollars; SA | pending live validation |
| US_REVOLVING_CREDIT | REVOLSL | monthly; millions dollars; SA | pending live validation |
| US_DURABLE_GOODS_ORDERS | DGORDER | monthly; millions dollars; SA | pending live validation |
| US_CORE_CAPITAL_GOODS_ORDERS | NEWORDER | monthly; millions dollars; SA | exclusion scope review required |
| US_MANUFACTURING_CAPACITY | CUMFNS | monthly; percent; SA | pending live validation |
| US_HOUSING_STARTS | HOUST | monthly; thousands units; SAAR | pending live validation |
| US_BUILDING_PERMITS | PERMIT | monthly; thousands units; SAAR | pending live validation |
| US_BUSINESS_INVENTORIES | BUSINV | monthly; millions dollars; SA | pending live validation |
| US_INVENTORY_SALES_RATIO | ISRATIO | monthly; ratio; SA | pending live validation |

Mandatory review points after the first live run:

- `AHETPI`: all employees versus production/nonsupervisory worker coverage.
- `NEWORDER`: nondefense capital goods and aircraft exclusion.
- `PPIACO`: all-commodities scope and seasonal-adjustment status.
- `AWHMAN`: covered manufacturing employee population.
- `RRSFS`: real retail and food-services sales semantics.

If any differs, keep the configured ID, record `valid_with_warning` or `invalid`, attach
the report, and wait for the user to approve any configuration change.
