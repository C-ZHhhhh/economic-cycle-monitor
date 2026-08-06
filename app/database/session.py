"""Engine, session, and lightweight readiness helpers."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker


def create_database_engine(database_url: str) -> Engine:
    """Create a small, health-checked SQLAlchemy engine."""
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=1,
        pool_recycle=1800,
    )


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Build a session factory bound to the supplied engine."""
    if engine is None:
        raise ValueError("An explicit database engine is required")
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def db_session(engine: Engine | None = None) -> Iterator[Session]:
    """Provide a transactional session for command-line operations."""
    session = get_session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database(engine: Engine | None = None) -> None:
    """Raise if PostgreSQL cannot execute a minimal query."""
    if engine is None:
        raise ValueError("An explicit database engine is required")
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
