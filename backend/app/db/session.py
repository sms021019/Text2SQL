"""Async engine construction for the app's database connections."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine(url: str, **kw: Any) -> AsyncEngine:
    """Create an `AsyncEngine` for `url`.

    Thin wrapper over `create_async_engine` that pins `pool_pre_ping=True`
    (so a connection dropped by the DB or a proxy is detected and replaced
    rather than handed back broken) as the one opinionated default; anything
    else is passed straight through via `kw`.
    """
    return create_async_engine(url, pool_pre_ping=True, **kw)
