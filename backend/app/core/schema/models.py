"""Plain-Python schema graph models.

`app/core/**` must never import fastapi/redis/app.api (see
`app/core/errors.py`), so this module only depends on pydantic.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel


class Column(BaseModel):
    name: str
    type: str
    nullable: bool
    comment: str | None
    is_pk: bool


class ForeignKey(BaseModel):
    column: str
    ref_table: str
    ref_column: str


class Table(BaseModel):
    name: str
    comment: str | None
    columns: list[Column]
    foreign_keys: list[ForeignKey]

    def summary(self) -> str:
        cols = ", ".join(c.name for c in self.columns)
        if self.comment:
            comment = self.comment if self.comment.endswith(".") else f"{self.comment}."
            return f"{self.name}: {comment} columns: {cols}"
        return f"{self.name}: columns: {cols}"


class SchemaGraph(BaseModel):
    tables: dict[str, Table]
    version: str

    @classmethod
    def build(cls, tables: dict[str, Table]) -> SchemaGraph:
        # Imported lazily to avoid a models.py <-> render.py import cycle:
        # render.py imports the model classes from this module at import
        # time, and this method needs render_ddl only when it runs.
        from app.core.schema.render import render_ddl

        draft = cls(tables=tables, version="")
        ddl = render_ddl(draft, sorted(tables))
        version = hashlib.sha256(ddl.encode()).hexdigest()[:12]
        return cls(tables=tables, version=version)

    def neighbors(self, table: str) -> set[str]:
        """Tables linked to `table` by a foreign key, in either direction.

        Excludes `table` itself, so a self-referencing FK (e.g.
        categories.parent_id -> categories.id) does not make a table its
        own neighbor.
        """
        this = self.tables[table]
        out = {fk.ref_table for fk in this.foreign_keys if fk.ref_table != table}
        incoming = {
            name
            for name, other in self.tables.items()
            if name != table and any(fk.ref_table == table for fk in other.foreign_keys)
        }
        return out | incoming
