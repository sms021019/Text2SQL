"""Programmatic entry point for running the app DB's Alembic migrations,
used by both the Task 12 startup entrypoint and the integration test --
neither wants to shell out to the `alembic` CLI.
"""

from __future__ import annotations

import pathlib

from alembic.config import Config

from alembic import command

__all__ = ["upgrade_to_head"]

_ALEMBIC_INI = pathlib.Path(__file__).resolve().parents[2] / "alembic.ini"


def upgrade_to_head(db_url: str) -> None:
    """Run `alembic upgrade head` against `db_url` (an async `+asyncpg` URL,
    converted here to the sync `+psycopg` form Alembic's migration runner
    needs). Safe to call repeatedly -- Alembic no-ops once the DB is
    already at head.
    """
    sync_url = db_url.replace("+asyncpg", "+psycopg")
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", sync_url)
    command.upgrade(config, "head")
