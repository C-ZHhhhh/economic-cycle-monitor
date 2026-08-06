"""FastAPI-specific resource dependencies."""

from collections.abc import Generator

from fastapi import Request
from sqlalchemy.orm import Session


def get_request_session(request: Request) -> Generator[Session, None, None]:
    """Yield a Session bound to the current application instance."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
