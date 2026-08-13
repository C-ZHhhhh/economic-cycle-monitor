# Stage 2B-4 Federal Reserve Board and DOL candidate mappings

This stage adds offline-tested production transports and parsers. Each mapping remains
`pending_review` until its independent Live Gate is completed; completed mappings are promoted
individually and never by implication from another provider or release.

## Federal Reserve Board

| Indicator | Release | Canonical identity | Production transport | Unit normalization |
|---|---|---|---|---|
| `US_INDUSTRIAL_PRODUCTION` | G.17 | `IP.B50001.S` (`B50001`) | Static `ip_sa.txt` plus current documentation | Validated configured base; currently 2017=100 |
| `US_MANUFACTURING_CAPACITY` | G.17 | `CAPUTL.B00004.S` (`B00004`) | Static `utl_sa.txt` plus current documentation | Percent of capacity, identity |
| `US_CONSUMER_CREDIT` | G.19 | `CCOUT / DTCTL.M` | Complete official XML/SDMX release | Board billions × 1000 to internal millions |
| `US_REVOLVING_CREDIT` | G.19 | `CCOUT / DTCTLR.M` | Complete official XML/SDMX release | Board billions × 1000 to internal millions |

The runtime has no Data Download Program package identifier, package hash, or Build/Choose
request. G.17 requires both canonical and transport identities. `B00004` is Manufacturing (SIC),
matching FRED `CUMFNS`; `GMF` is Manufacturing (NAICS) and is rejected for this target.
`CAP.GMF.S` is also explicitly rejected because it is a capacity index rather than capacity
utilization. G.19 matches the complete SeriesKey and rejects flow, percent-change,
annual-rate-change, NSA, frequency, unit, and multiplier variants.

The G.17 base year is read from official release metadata on every validation/reconciliation. A
change from configured 2017 to a returned 2022 does not invalidate the economic identity or
silently rebase data: it raises `unexpected_base_year`, emits a quality event, blocks the write,
and requires review before YAML or stored observations change.

## Department of Labor

| Indicator | Report | Required economic identity | Candidate path status |
|---|---|---|---|
| `US_INITIAL_CLAIMS` | National Weekly Claims `r539cy` | United States / Regular State Program / Initial Claims / SA / Weekly / Persons | Pending Live Gate |
| `US_CONTINUING_CLAIMS` | National Weekly Claims `r539cy` | United States / Regular State Program / Insured Unemployment official equivalent / SA / Weekly / Persons | Pending Live Gate |

Production supports strict configured XML paths but deliberately leaves the candidate paths and
wire values empty until the DOL Live Gate. It never guesses tags. Observation dates come from each
series' week-ending field, not the release date. Operational outages and maintenance responses are
classified as source degradation and do not change a mapping to invalid.

ETA 539 raw state data is not used as the production ICSA/CCSA series. It remains outside this
stage's ingestion scope and may only be reviewed later as supporting or audit evidence.

## Audit and revision policy

`observation_date`, `release_date`, and `retrieved_at` remain distinct. Current release dates are
stored in `raw_observations.source_details_json`; revisions use the existing
`observation_vintages.release_date`. When an official historical file provides no release date,
the value remains null and `release_date_status=not_provided` is persisted.

G.17 and target G.19 histories use complete release reconciliation. DOL uses configured recent and
annual reconciliation settings. Only a same-source, same-identity, same-observation-date value
change creates a revision. Cross-source differences never create provider revisions.

No database model change or Alembic migration is required.

## Live gates

The next authorized actions are independent and sequential:

1. Federal Reserve Gate: validate only `US_INDUSTRIAL_PRODUCTION` first.
2. DOL Gate: validate only `US_INITIAL_CLAIMS` first and review returned XML paths before editing
   either DOL mapping.

No live Federal Reserve or DOL request was made during implementation.
