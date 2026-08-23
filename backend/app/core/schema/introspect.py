"""Introspect a live Postgres database into a `SchemaGraph`.

The async `Inspector` needs the actual reflection calls (`get_table_names`,
`get_columns`, ...) to run inside `Connection.run_sync`, since SQLAlchemy's
DBAPI-level reflection is synchronous. See
https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#synopsis-orm
for the `run_sync` pattern.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.schema.models import Column, ForeignKey, SchemaGraph, Table

logger = logging.getLogger(__name__)


def _introspect_sync(conn: Connection) -> SchemaGraph:
    insp = inspect(conn)
    tables: dict[str, Table] = {}

    for table_name in insp.get_table_names():
        pk_columns = set(insp.get_pk_constraint(table_name).get("constrained_columns") or [])

        # Composite (multi-column) foreign keys are unsupported in v1 -- the
        # ForeignKey model has a single `column`/`ref_column` pair -- so skip
        # them rather than silently truncating to just the first column.
        foreign_keys = []
        for fk in insp.get_foreign_keys(table_name):
            constrained = fk["constrained_columns"]
            referred = fk["referred_columns"]
            if len(constrained) != 1 or len(referred) != 1:
                logger.warning(
                    "skipping composite foreign key on %s.%s -> %s.%s (unsupported in v1)",
                    table_name,
                    constrained,
                    fk["referred_table"],
                    referred,
                )
                continue
            foreign_keys.append(
                ForeignKey(
                    column=constrained[0],
                    ref_table=fk["referred_table"],
                    ref_column=referred[0],
                )
            )

        columns = []
        for col in insp.get_columns(table_name):
            comment = col.get("comment")
            enums = getattr(col["type"], "enums", None)
            if enums:
                enum_note = f"one of: {', '.join(enums)}."
                comment = f"{enum_note} {comment}" if comment else enum_note
            columns.append(
                Column(
                    name=col["name"],
                    type=str(col["type"]),
                    nullable=bool(col["nullable"]),
                    comment=comment,
                    is_pk=col["name"] in pk_columns,
                )
            )

        table_comment = insp.get_table_comment(table_name).get("text")
        tables[table_name] = Table(
            name=table_name,
            comment=table_comment,
            columns=columns,
            foreign_keys=foreign_keys,
        )

    return SchemaGraph.build(tables)


async def introspect(engine: AsyncEngine) -> SchemaGraph:
    async with engine.connect() as conn:
        return await conn.run_sync(_introspect_sync)
