"""Top-level API router: mounts `/healthz` and `/readyz` at the root and the
v1 endpoints under `/api/v1`.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import health, query, schema

__all__ = ["router"]

router = APIRouter()
router.include_router(health.router, tags=["health"])
router.include_router(query.router, prefix="/api/v1", tags=["query"])
router.include_router(schema.router, prefix="/api/v1", tags=["schema"])
