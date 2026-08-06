# US Macro Cycle Agent — Stage 1 Requirements

## Scope

Stage 1 establishes the project skeleton and data foundation for a personal US
macro-cycle monitoring agent. It does not collect real economic data, calculate
features, score modules, infer regimes, run nowcasts, call an LLM, or schedule work.

## Stable output contract

The canonical machine-readable result is `agents.macro.schemas.MacroCycleResult`.
Future synthesis agents must consume this structure rather than parsing prose.

## Deferred integrations

- FRED, BLS, BEA, and Census data collection
- DeepSeek API calls
- Indicators, revisions, feature calculations, and scoring
- Regime inference, nowcasting, scheduling, and additional agents

Any change to the database models requires an Alembic migration. Any change to
the result contract requires tests and an explicit versioning decision.
