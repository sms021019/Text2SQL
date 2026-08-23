"""SELECT-only SQL guard built on the sqlglot AST.

Policy
------
`guard_sql()` parses the candidate SQL with sqlglot's Postgres dialect and
walks the resulting tree. It never pattern-matches on the raw string, so
`SELECT * FROM orders -- ; DROP TABLE orders` is (correctly) accepted while
`SELECT 1; DROP TABLE orders` is rejected -- a regex guard gets both wrong.

A statement is accepted only if all of the following hold:

* it parses, and parses to exactly one statement (a trailing `;` is fine);
* sqlglot did not fall back to an opaque ``exp.Command`` (``EXPLAIN``,
  ``VACUUM``, ... ) -- those are unanalysable, so they are refused;
* the root node is a ``SELECT`` or a set operation (``UNION`` / ``INTERSECT``
  / ``EXCEPT``), optionally with a ``WITH`` clause, and every arm of a set
  operation is itself a query;
* no CTE body is a write (``WITH x AS (DELETE ... RETURNING *)``), and no
  DML/DDL node appears anywhere in the tree;
* there is no ``SELECT ... INTO`` and no row lock (``FOR UPDATE`` /
  ``FOR SHARE``);
* no call to a function in :data:`FORBIDDEN_FUNCTIONS` (sleep / file / network
  / session-state primitives), matched case-insensitively and with any
  ``pg_catalog.`` qualifier stripped;
* every referenced table is either a CTE alias defined in the same statement
  or a known table, unqualified or qualified with the ``public`` schema.

On acceptance the statement is normalised back to Postgres SQL with a row
cap applied to the *outermost* query only: absent ``LIMIT`` gets `max_rows`
injected, a larger or non-literal ``LIMIT`` is clamped to `max_rows`, and a
smaller one is left alone. Inner subquery limits are untouched -- they are
part of the query's meaning, not its blast radius.

Security boundary
-----------------
**This guard is not the security boundary.** The database connection used to
run generated SQL is a read-only role with no write grants, a statement
timeout, and no access to tables outside the exposed schema; that role is
what actually makes a malicious statement harmless. The guard exists to
(a) fail fast and cheaply with an explainable reason instead of burning a
round trip on a query the database would reject anyway, and (b) give the
user a precise message ("unknown table: secrets") rather than a raw driver
error. Treat any bypass of this module as a UX bug, not a breach -- and keep
the read-only role as the thing you rely on.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from app.core.errors import DomainError

__all__ = ["FORBIDDEN_FUNCTIONS", "GuardError", "guard_sql"]

DIALECT = "postgres"

#: Postgres functions that let a read-only session sleep, touch the
#: filesystem, open outbound connections, or mutate session/backend state.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "current_setting",
        "dblink",
        "dblink_exec",
        "lo_export",
        "lo_import",
        "pg_cancel_backend",
        "pg_ls_dir",
        "pg_read_binary_file",
        "pg_read_file",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_stat_file",
        "pg_terminate_backend",
        "query_to_xml",
        "set_config",
    }
)

#: Node types that mutate data or schema. ``exp.DML``/``exp.DDL`` are
#: sqlglot's marker bases; ``Drop``/``TruncateTable``/``Set`` do not inherit
#: from either in sqlglot 30, so they are listed explicitly. Left unannotated
#: on purpose: the mixins are not ``Expression`` subclasses, so a
#: ``tuple[type[exp.Expression], ...]`` annotation would not type-check.
_WRITE_NODES = (
    exp.DDL,
    exp.DML,
    exp.Drop,
    exp.Set,
    exp.TruncateTable,
)

#: Node types allowed as the root of a statement and as a set-operation arm.
_QUERY_NODES: tuple[type[exp.Expression], ...] = (exp.Select, exp.SetOperation)


class GuardError(DomainError):
    """Raised when a statement violates the SELECT-only policy.

    `reason` is a stable machine-readable code the API layer maps to an error
    response; `detail` is the human-facing explanation.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


def guard_sql(sql: str, known_tables: set[str], *, max_rows: int) -> str:
    """Validate `sql` as a read-only query and return it with a row cap.

    Args:
        sql: Candidate SQL, typically LLM-generated.
        known_tables: Table names the query may reference (case-insensitive).
        max_rows: Row cap applied to the outermost query.

    Returns:
        The statement rendered as Postgres SQL with `LIMIT` applied.

    Raises:
        GuardError: If the statement violates the policy. Fails closed --
            anything the parser cannot make sense of is rejected.
    """
    expr = _parse_single(sql)
    _reject_non_select_set_op_arms(expr)
    _reject_writes(expr)
    _reject_into_and_locks(expr)
    _reject_forbidden_functions(expr)
    _reject_unknown_tables(expr, known_tables)
    return _apply_limit(expr, max_rows).sql(dialect=DIALECT)


# ---------------------------------------------------------------------------
# Stage 1: parse
# ---------------------------------------------------------------------------


def _parse_single(sql: str) -> exp.Query:
    try:
        parsed = sqlglot.parse(sql, read=DIALECT)
    except Exception as exc:  # sqlglot raises ParseError/TokenError subclasses
        raise GuardError("parse", f"could not parse SQL: {exc}") from exc

    # `None` entries and bare `exp.Semicolon` nodes are empty statements, i.e.
    # a trailing `;` or `; --`. They carry no SQL, so they do not count.
    statements = [s for s in parsed if s is not None and not isinstance(s, exp.Semicolon)]

    if not statements:
        raise GuardError("parse", "no SQL statement found")
    if len(statements) > 1:
        kinds = ", ".join(type(s).__name__ for s in statements)
        raise GuardError(
            "multi_statement",
            f"expected a single statement, found {len(statements)} ({kinds})",
        )

    expr = statements[0]
    if isinstance(expr, exp.Command):
        # sqlglot's fallback for syntax it cannot model (EXPLAIN, VACUUM, ...).
        # It is opaque to the AST walk below, so it can never be cleared.
        raise GuardError("parse", f"unsupported or unanalysable statement: {expr.name or sql!r}")
    if not isinstance(expr, _QUERY_NODES) or not isinstance(expr, exp.Query):
        raise GuardError("not_select", f"expected a SELECT, found {type(expr).__name__.upper()}")
    return expr


# ---------------------------------------------------------------------------
# Stage 2: shape of the query
# ---------------------------------------------------------------------------


def _reject_non_select_set_op_arms(expr: exp.Query) -> None:
    """Every arm of a set operation must itself be a query."""
    for node in expr.find_all(exp.SetOperation):
        for side in ("this", "expression"):
            arm = node.args.get(side)
            if arm is None:
                continue
            if isinstance(arm, exp.Subquery):
                arm = arm.this
            if not isinstance(arm, _QUERY_NODES):
                raise GuardError(
                    "set_op_non_select",
                    f"set operation arm is a {type(arm).__name__.upper()}, not a SELECT",
                )


def _reject_writes(expr: exp.Query) -> None:
    """Reject DML/DDL anywhere, reporting CTE bodies separately."""
    for cte in expr.find_all(exp.CTE):
        body = cte.this
        if isinstance(body, exp.Subquery):
            body = body.this
        if not isinstance(body, _QUERY_NODES):
            raise GuardError(
                "write_cte",
                f"CTE {cte.alias or '<anonymous>'} is a "
                f"{type(body).__name__.upper()}, not a SELECT",
            )

    for node in expr.find_all(*_WRITE_NODES):
        kind = type(node).__name__.upper()
        if _has_ancestor(node, exp.CTE):
            raise GuardError("write_cte", f"{kind} inside a CTE")
        raise GuardError("not_select", f"{kind} is not allowed in a read-only query")

    for node in expr.find_all(exp.Command):
        raise GuardError("not_select", f"unanalysable command inside the query: {node.name!r}")


def _reject_into_and_locks(expr: exp.Query) -> None:
    into = expr.find(exp.Into)
    if into is not None:
        raise GuardError("into", "SELECT ... INTO writes a new table")

    lock = expr.find(exp.Lock)
    if lock is not None or expr.args.get("locks"):
        raise GuardError("lock", "row locks (FOR UPDATE / FOR SHARE) are not allowed")


# ---------------------------------------------------------------------------
# Stage 3: functions and tables
# ---------------------------------------------------------------------------


def _reject_forbidden_functions(expr: exp.Query) -> None:
    for node in expr.find_all(exp.Func):
        for name in _function_names(node):
            if name in FORBIDDEN_FUNCTIONS:
                raise GuardError("forbidden_function", f"function {name}() is not allowed")


def _function_names(node: exp.Func) -> set[str]:
    """Every name a `Func` node could be known by, normalised.

    ``exp.Anonymous`` carries the literal source name in ``.name``; typed
    ``Func`` subclasses expose their SQL spellings via ``sql_names()`` (their
    ``.name`` is the first argument instead). Any ``pg_catalog.`` qualifier is
    stripped so `pg_catalog.pg_sleep` matches `pg_sleep`.
    """
    if isinstance(node, exp.Anonymous):
        raw = {str(node.name)}
    else:
        raw = set(node.sql_names()) | {node.key}
    return {name.lower().rsplit(".", 1)[-1] for name in raw if name}


def _reject_unknown_tables(expr: exp.Query, known_tables: set[str]) -> None:
    known = {name.lower() for name in known_tables}
    cte_aliases = {cte.alias.lower() for cte in expr.find_all(exp.CTE) if cte.alias}

    for table in expr.find_all(exp.Table):
        name = table.name
        if not name:
            # Table-valued function (e.g. `FROM generate_series(1, 10) AS g`).
            # The callable itself is vetted by the forbidden-function pass.
            continue
        if name.lower() in cte_aliases:
            continue

        schema = (table.db or "").lower()
        catalog = (table.catalog or "").lower()
        if catalog or (schema and schema != "public"):
            qualified = ".".join(part for part in (catalog, schema, name) if part)
            raise GuardError("unknown_table", f"unknown table: {qualified}")
        if name.lower() not in known:
            raise GuardError("unknown_table", f"unknown table: {name}")


# ---------------------------------------------------------------------------
# Stage 4: row cap
# ---------------------------------------------------------------------------


def _apply_limit(expr: exp.Query, max_rows: int) -> exp.Query:
    """Cap the outermost query at `max_rows`, leaving smaller limits alone."""
    limit = expr.args.get("limit")
    if isinstance(limit, exp.Limit):
        current = _literal_int(limit.expression)
        if current is not None and 0 <= current <= max_rows:
            return expr
    # Absent, non-literal (`LIMIT ALL`, `LIMIT $1`), negative, or too large.
    return expr.limit(max_rows)


def _literal_int(node: exp.Expression | None) -> int | None:
    if not isinstance(node, exp.Literal) or node.is_string:
        return None
    try:
        return int(node.name)
    except ValueError:
        return None


def _has_ancestor(node: exp.Expr, kind: type[exp.Expression]) -> bool:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, kind):
            return True
        parent = parent.parent
    return False
