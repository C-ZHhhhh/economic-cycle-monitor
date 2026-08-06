"""Shared pytest fixtures."""

import os
from collections.abc import Generator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Return isolated valid Stage 1 settings."""
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="postgresql+psycopg://macro:macro@localhost:5432/macro_test",
        llm_mode="mock",
    )


def _require_postgres_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        if os.getenv("CI"):
            pytest.fail("TEST_DATABASE_URL is required in CI")
        pytest.skip("TEST_DATABASE_URL is not configured")
    return url


@pytest.fixture(scope="session")
def postgres_engine() -> Generator[Engine, None, None]:
    """Migrate and expose the dedicated integration-test database."""
    url = _require_postgres_url()
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        engine.dispose()
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL is unavailable in CI: {type(exc).__name__}")
        pytest.skip("PostgreSQL integration database is unavailable")

    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def postgres_session(postgres_engine: Engine) -> Generator[Session, None, None]:
    """Provide a rollback-only transaction for one integration test."""
    connection = postgres_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
