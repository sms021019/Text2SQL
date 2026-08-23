"""Liveness/readiness probes.

`/healthz` is unconditional (200 as long as the process is up); `/readyz`
additionally requires the lifespan to have finished loading the schema
(`app.state.ready`) and both databases to answer `SELECT 1`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

__all__ = ["router"]

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    state = request.app.state
    if not getattr(state, "ready", False):
        response.status_code = 503
        return {"status": "not ready"}

    for engine in (state.target_engine, state.app_engine):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 -- any DB failure means "not ready"
            response.status_code = 503
            return {"status": "not ready"}

    return {"status": "ok"}
