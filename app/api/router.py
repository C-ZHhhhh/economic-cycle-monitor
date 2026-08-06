"""Versioned API router composition."""

from fastapi import APIRouter

from app.api.routes import (
    data_quality,
    data_sources,
    health,
    indicators,
    macro,
    runs,
    sync_runs,
    system,
)

router = APIRouter()
router.include_router(health.router)
router.include_router(system.router, prefix="/api/v1/system", tags=["system"])
router.include_router(macro.router, prefix="/api/v1/macro", tags=["macro"])
router.include_router(runs.router, prefix="/api/v1/runs", tags=["runs"])
router.include_router(indicators.router, prefix="/api/v1/indicators", tags=["indicators"])
router.include_router(data_sources.router, prefix="/api/v1/data-sources", tags=["data-sources"])
router.include_router(data_quality.router, prefix="/api/v1/data-quality", tags=["data-quality"])
router.include_router(sync_runs.router, prefix="/api/v1/sync-runs", tags=["sync-runs"])
