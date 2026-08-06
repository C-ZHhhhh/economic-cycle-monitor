"""Versioned API router composition."""

from fastapi import APIRouter

from app.api.routes import health, macro, runs, system

router = APIRouter()
router.include_router(health.router)
router.include_router(system.router, prefix="/api/v1/system", tags=["system"])
router.include_router(macro.router, prefix="/api/v1/macro", tags=["macro"])
router.include_router(runs.router, prefix="/api/v1/runs", tags=["runs"])
