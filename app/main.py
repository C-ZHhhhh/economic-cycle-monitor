"""FastAPI application factory and local entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app import __version__
from app.api.errors import install_error_handlers
from app.api.router import router
from app.database.session import create_database_engine, get_session_factory
from app.logging import configure_logging
from app.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create a configured application instance suitable for tests or Uvicorn."""
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    engine = create_database_engine(resolved_settings.database_url)
    session_factory = get_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    application = FastAPI(
        title="Economic Cycle Monitor",
        description="Stage 2 FRED data foundation for the US Macro Cycle Agent.",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.engine = engine
    application.state.session_factory = session_factory

    @application.middleware("http")
    async def add_request_id(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    install_error_handlers(application)
    application.include_router(router)
    return application


app = create_app()
