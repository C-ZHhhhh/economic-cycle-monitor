# Repository Instructions

## Project goal and current scope

This repository is the sole codebase for a personal global economic-cycle
multi-agent monitoring system. The current scope is only Stage 2 of the US Macro
Cycle Agent: FRED ingestion, indicator registration, revisions, vintages, and data
quality. Do not implement other agents or Stage 3 features, scoring, or regime
functionality without explicit approval.

## Runtime and resource constraints

- Use Python 3.12.
- Support Apple M1 hardware with 8GB RAM.
- Run one PostgreSQL 16 container with a 512MB memory limit.
- Run Uvicorn with one worker.
- Do not add Redis, Celery, Kafka, Kubernetes, local LLMs, vector databases, or
  concurrent agents.
- Keep analysis, LLM, and backtest concurrency at one.

## Code standards

- Use modern typed Python, Pydantic v2, and SQLAlchemy 2 declarative mappings.
- Public functions and core classes require type hints and docstrings.
- Use UTC-aware datetimes.
- Do not use bare `except` clauses or suppress exceptions silently.
- Keep database Session lifetimes explicit and avoid circular imports.
- Avoid abstractions that do not have a current Stage 2 use.
- Never hard-code indicator weights, scoring thresholds, API keys, or an LLM model
  name in Python. Version weights and thresholds in `config/`.
- Agent output must validate against the canonical Pydantic schema.
- Tests must not call a real external API unless explicitly marked `live` and
  manually invoked.
- Never change a configured FRED Series ID automatically after a metadata warning.

## Security

- Never commit `.env`, credentials, tokens, database backups, or generated reports.
- Never print API keys or database credentials in logs, CLI output, or API errors.
- `.env.example` must contain placeholders only.

## Required checks

Run all of these before committing:

```bash
ruff format --check .
ruff check .
mypy app agents
pytest
```

Do not bypass, disable, or weaken tests to make a change pass.

## Formatting and migrations

Format Python with:

```bash
ruff format .
```

Apply migrations with:

```bash
alembic upgrade head
```

Every database model change must include a reviewed Alembic migration. Do not use
`Base.metadata.create_all()` as the production migration mechanism.

## Stage gates

At the end of each project stage, stop, report validation results and known risks,
and wait for explicit confirmation before starting the next stage.
