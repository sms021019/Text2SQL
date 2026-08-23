"""Render a `SchemaGraph` (or a subset of it) to canonical DDL text.

The output is used both as a stable input to `SchemaGraph.version` (hashed
over all tables in sorted order) and as schema context handed to an LLM
(rendered over just the tables relevant to a question).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.schema.models import Column, SchemaGraph, Table


def _column_comment(col: Column) -> str | None:
    if col.is_pk:
        return f"primary key. {col.comment}" if col.comment else "primary key"
    return col.comment


def _render_table(table: Table) -> str:
    lines: list[tuple[str, str | None]] = []

    pk_cols: list[str] = []
    for col in table.columns:
        code = f"{col.name} {col.type}"
        if not col.nullable:
            code += " NOT NULL"
        if col.is_pk:
            pk_cols.append(col.name)
        lines.append((code, _column_comment(col)))

    if pk_cols:
        lines.append((f"PRIMARY KEY ({', '.join(pk_cols)})", None))

    for fk in table.foreign_keys:
        lines.append(
            (f"FOREIGN KEY ({fk.column}) REFERENCES {fk.ref_table}({fk.ref_column})", None)
        )

    body: list[str] = []
    last = len(lines) - 1
    for i, (code, comment) in enumerate(lines):
        suffix = "," if i < last else ""
        if comment:
            body.append(f"  {code}{suffix} -- {comment}")
        else:
            body.append(f"  {code}{suffix}")

    header_comment = f" -- {table.comment}" if table.comment else ""
    return f"CREATE TABLE {table.name} ({header_comment}\n" + "\n".join(body) + "\n);"


def render_ddl(graph: SchemaGraph, tables: Iterable[str]) -> str:
    """Render `tables` (in the given order) as DDL. Raises KeyError for an
    unknown table name."""
    return "\n\n".join(_render_table(graph.tables[name]) for name in tables)
