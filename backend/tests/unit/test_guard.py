"""Tests for the sqlglot AST SQL guard.

The guard is the project's headline safety feature, so this is deliberately
the largest suite in the repo. Two themes run through it:

1. *Rejections* are asserted on `GuardError.reason`, not on the message, so
   the API layer can map reasons to stable error codes.
2. *Acceptances* include cases a regex-based guard would get wrong (SQL
   keywords inside comments and string literals). Those are the proof that
   parsing beats pattern matching.
"""

import pytest

from app.core.errors import DomainError
from app.core.sql import GuardError, guard_sql

KNOWN_TABLES = {"orders", "customers", "order_items"}
MAX_ROWS = 500


def guard(sql: str, *, max_rows: int = MAX_ROWS) -> str:
    return guard_sql(sql, KNOWN_TABLES, max_rows=max_rows)


# --------------------------------------------------------------------------
# GuardError shape
# --------------------------------------------------------------------------


def test_guard_error_is_a_domain_error() -> None:
    err = GuardError("parse", "could not parse")
    assert isinstance(err, DomainError)
    assert err.reason == "parse"
    assert err.detail == "could not parse"
    assert str(err) == "parse: could not parse"


# --------------------------------------------------------------------------
# Rejections (cases from the task brief, verbatim, plus adversarial extras)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        # --- not a SELECT at all
        ("DELETE FROM orders", "not_select"),
        ("UPDATE orders SET status = 'paid'", "not_select"),
        ("INSERT INTO orders (id) VALUES (1)", "not_select"),
        ("DROP TABLE orders", "not_select"),
        ("TRUNCATE orders", "not_select"),
        ("CREATE TABLE t AS SELECT * FROM orders", "not_select"),
        ("COPY orders TO PROGRAM 'ls'", "not_select"),
        ("VALUES (1)", "not_select"),
        # sqlglot models SET as a real statement (exp.Set), not a Command
        # fallback, so `not_select` is the accurate reason rather than `parse`.
        ("SET search_path = public", "not_select"),
        # --- stacked statements
        ("SELECT 1; DROP TABLE orders", "multi_statement"),
        ("SELECT * FROM orders; SELECT * FROM customers", "multi_statement"),
        # --- write inside a CTE
        ("WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x", "write_cte"),
        (
            "WITH x AS (UPDATE orders SET status = 'paid' RETURNING *) SELECT * FROM x",
            "write_cte",
        ),
        (
            "WITH x AS (INSERT INTO orders (id) VALUES (1) RETURNING *) SELECT * FROM x",
            "write_cte",
        ),
        # --- SELECT ... INTO
        ("SELECT * INTO t FROM orders", "into"),
        ("SELECT * INTO TEMP t FROM orders", "into"),
        # --- row locks
        ("SELECT * FROM orders FOR UPDATE", "lock"),
        ("SELECT * FROM orders FOR SHARE", "lock"),
        # --- forbidden functions
        ("SELECT pg_sleep(10)", "forbidden_function"),
        ("SELECT pg_read_file('/etc/passwd')", "forbidden_function"),
        ("SELECT set_config('a','b',false)", "forbidden_function"),
        ("SELECT * FROM dblink('x','y') AS t(a int)", "forbidden_function"),
        ("SELECT current_setting('is_superuser')", "forbidden_function"),
        ("SELECT lo_import('/etc/passwd')", "forbidden_function"),
        ("SELECT pg_terminate_backend(1)", "forbidden_function"),
        # case-insensitive
        ("SELECT PG_SLEEP(10)", "forbidden_function"),
        ("SELECT Pg_Read_File('/etc/passwd')", "forbidden_function"),
        # schema-qualified into pg_catalog
        ("SELECT pg_catalog.pg_sleep(10)", "forbidden_function"),
        # buried in a subquery / CTE / union arm
        ("SELECT * FROM orders WHERE id IN (SELECT pg_sleep(1))", "forbidden_function"),
        (
            "WITH x AS (SELECT pg_read_file('/etc/passwd') AS a) SELECT * FROM x",
            "forbidden_function",
        ),
        ("SELECT 1 UNION SELECT pg_sleep(1)", "forbidden_function"),
        # --- unknown tables
        ("SELECT * FROM secrets", "unknown_table"),
        ("SELECT * FROM orders JOIN secrets ON TRUE", "unknown_table"),
        ("SELECT (SELECT count(*) FROM secrets) FROM orders", "unknown_table"),
        ("SELECT * FROM pg_catalog.pg_shadow", "unknown_table"),
        ("SELECT * FROM information_schema.tables", "unknown_table"),
        # right table name, wrong schema
        ("SELECT * FROM other.orders", "unknown_table"),
        # union arm referencing an unknown table
        ("SELECT id FROM orders UNION SELECT id FROM secrets", "unknown_table"),
        # --- unparseable / opaque commands
        ("EXPLAIN SELECT * FROM orders", "parse"),
        ("VACUUM", "parse"),
        ("this is not sql at all", "parse"),
        ("", "parse"),
        ("   ", "parse"),
    ],
)
def test_rejects(sql: str, reason: str) -> None:
    with pytest.raises(GuardError) as excinfo:
        guard(sql)
    assert excinfo.value.reason == reason


def test_union_with_delete_arm_is_rejected() -> None:
    """Brief allows either `parse` or `not_select` here - just never accepted."""
    with pytest.raises(GuardError) as excinfo:
        guard("SELECT 1 UNION ALL DELETE FROM orders")
    assert excinfo.value.reason in {"parse", "not_select", "set_op_non_select"}


# --------------------------------------------------------------------------
# Forbidden-function families (exact names + prefix rules)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn",
    [
        # dblink* — outbound connections
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_connect_u",
        "dblink_send_query",
        "dblink_get_result",
        "dblink_open",
        "dblink_fetch",
        # lo_* — large objects (read and write the filesystem)
        "lo_import",
        "lo_export",
        "lo_creat",
        "lo_put",
        "lo_get",
        "lo_unlink",
        "lo_from_bytea",
        # advisory locks — session-wide, outlive the statement
        "pg_advisory_lock",
        "pg_advisory_lock_shared",
        "pg_advisory_xact_lock",
        "pg_advisory_unlock",
        "pg_try_advisory_lock",
        "pg_try_advisory_xact_lock",
        # sequence mutation — writes wearing a read's clothes
        "nextval",
        "setval",
        "currval",
        "lastval",
        # filesystem / directory
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_ls_waldir",
        "pg_stat_file",
        "pg_file_write",
        "pg_file_unlink",
        "pg_logdir_ls",
        # backend and cluster state
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_stat_reset",
        "pg_stat_reset_shared",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_backend_pid",
        # sleeps / timing oracles
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        # session state and exfiltration
        "set_config",
        "current_setting",
        "query_to_xml",
    ],
)
def test_forbidden_function_families(fn: str) -> None:
    with pytest.raises(GuardError) as excinfo:
        guard(f"SELECT {fn}(1)")
    assert excinfo.value.reason == "forbidden_function"


@pytest.mark.parametrize(
    "sql",
    [
        # The prefix rules must not degrade into blanket `pg_` blocking: these
        # are ordinary read-only introspection helpers.
        "SELECT pg_typeof(1) FROM orders",
        "SELECT pg_column_size(id) FROM orders",
    ],
)
def test_benign_pg_functions_are_not_over_blocked(sql: str) -> None:
    assert guard(sql).endswith("LIMIT 500")


def test_write_in_a_cte_nested_inside_a_subquery_is_rejected() -> None:
    """The CTE scan walks the whole tree, not just the root WITH clause."""
    with pytest.raises(GuardError) as excinfo:
        guard("SELECT * FROM (WITH y AS (DELETE FROM orders RETURNING *) SELECT * FROM y) AS t")
    assert excinfo.value.reason == "write_cte"


def test_lock_on_a_union_arm_is_rejected() -> None:
    with pytest.raises(GuardError) as excinfo:
        guard("SELECT id FROM orders UNION SELECT id FROM customers FOR UPDATE")
    assert excinfo.value.reason == "lock"


# --------------------------------------------------------------------------
# CTE alias scoping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # A CTE declared inside a subquery must not exempt a same-named real
        # table in an enclosing/sibling scope. Collecting aliases tree-wide
        # would accept all three of these.
        "SELECT * FROM secrets, (WITH secrets AS (SELECT 1 AS a) SELECT * FROM secrets) AS z",
        (
            "SELECT (SELECT count(*) FROM secrets) "
            "FROM (WITH secrets AS (SELECT 1 AS a) SELECT * FROM secrets) AS z"
        ),
        (
            "SELECT id FROM (WITH secrets AS (SELECT 1 AS id) SELECT * FROM secrets) AS z "
            "UNION SELECT id FROM secrets"
        ),
    ],
)
def test_cte_alias_does_not_leak_out_of_its_scope(sql: str) -> None:
    with pytest.raises(GuardError) as excinfo:
        guard(sql)
    assert excinfo.value.reason == "unknown_table"


@pytest.mark.parametrize(
    "sql",
    [
        # A qualified name can never bind to a CTE in Postgres -- it always
        # means the real table -- so a CTE alias must not exempt it from the
        # schema and known-table checks.
        "WITH secrets AS (SELECT 1 AS a) SELECT * FROM public.secrets",
        "WITH secrets AS (SELECT 1 AS a) SELECT * FROM other_schema.secrets",
    ],
)
def test_cte_alias_does_not_exempt_a_qualified_name(sql: str) -> None:
    with pytest.raises(GuardError) as excinfo:
        guard(sql)
    assert excinfo.value.reason == "unknown_table"


def test_qualified_known_table_and_cte_alias_coexist() -> None:
    """The unqualified alias still resolves; the qualified real table is
    checked on its own merits and passes because `orders` is known."""
    out = guard("WITH o AS (SELECT 1) SELECT * FROM public.orders, o")
    assert out.endswith("LIMIT 500")


def test_cte_alias_is_visible_inside_its_own_subquery_scope() -> None:
    """The flip side: within the scope that declares it, the alias resolves."""
    out = guard("SELECT * FROM (WITH t AS (SELECT 1 AS a) SELECT * FROM t) AS z")
    assert out.endswith("LIMIT 500")


def test_sibling_cte_is_visible_within_the_same_with_clause() -> None:
    out = guard("WITH a AS (SELECT * FROM orders), b AS (SELECT * FROM a) SELECT * FROM b")
    assert out.endswith("LIMIT 500")


def test_recursive_cte_self_reference_is_allowed() -> None:
    out = guard(
        "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 5) "
        "SELECT * FROM t"
    )
    assert out.endswith("LIMIT 500")


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------


def test_parenthesised_statement_is_unwrapped_and_limited() -> None:
    assert guard("(SELECT * FROM orders)") == "SELECT * FROM orders LIMIT 500"
    assert guard("((SELECT * FROM orders))") == "SELECT * FROM orders LIMIT 500"


def test_alter_is_rejected() -> None:
    with pytest.raises(GuardError) as excinfo:
        guard("ALTER TABLE orders ADD COLUMN x int")
    assert excinfo.value.reason == "not_select"


def test_set_op_non_select_is_reachable() -> None:
    """`TABLE orders` parses to an Alias, not a query, as a UNION arm."""
    with pytest.raises(GuardError) as excinfo:
        guard("TABLE orders UNION SELECT 1")
    assert excinfo.value.reason == "set_op_non_select"


def test_empty_known_tables_rejects_everything_with_a_from() -> None:
    with pytest.raises(GuardError) as excinfo:
        guard_sql("SELECT * FROM orders", set(), max_rows=MAX_ROWS)
    assert excinfo.value.reason == "unknown_table"


def test_column_named_like_a_forbidden_function_is_fine() -> None:
    """Only call sites are checked, not identifiers that merely share a name."""
    assert guard("SELECT pg_sleep FROM orders").endswith("LIMIT 500")


def test_rejection_detail_is_populated() -> None:
    with pytest.raises(GuardError) as excinfo:
        guard("SELECT * FROM secrets")
    assert excinfo.value.detail
    assert "secrets" in excinfo.value.detail


# --------------------------------------------------------------------------
# Acceptances - the "regex would have been wrong" cases
# --------------------------------------------------------------------------


def test_accepts_sql_keywords_inside_a_line_comment() -> None:
    """A regex guard scanning for `DROP` or `;` would reject this.

    The AST knows the payload is a comment, so it is harmless.
    """
    out = guard("SELECT * FROM orders -- ; DROP")
    assert out.endswith("LIMIT 500")


def test_accepts_sql_keywords_inside_a_block_comment() -> None:
    out = guard("SELECT * /* DELETE FROM orders; */ FROM orders")
    assert out.endswith("LIMIT 500")


def test_accepts_sql_keywords_inside_a_string_literal() -> None:
    out = guard("SELECT 'DROP TABLE orders' AS s FROM orders")
    assert "DROP TABLE orders" in out
    assert out.endswith("LIMIT 500")


def test_accepts_trailing_semicolon() -> None:
    """A single trailing `;` is a common LLM output habit, not a stacked
    statement."""
    assert guard("SELECT * FROM orders;").endswith("LIMIT 500")
    assert guard("SELECT * FROM orders; --").endswith("LIMIT 500")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders",
        "SELECT * FROM public.orders",
        "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id",
        "SELECT * FROM (SELECT * FROM orders) AS t",
        "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent",
        "WITH a AS (SELECT * FROM orders), b AS (SELECT * FROM a) SELECT * FROM b",
        "SELECT id FROM orders UNION SELECT id FROM customers",
        "SELECT id FROM orders INTERSECT SELECT id FROM customers",
        "SELECT id FROM orders EXCEPT SELECT id FROM customers",
        "SELECT count(*) FROM orders GROUP BY status HAVING count(*) > 1",
        "SELECT * FROM orders ORDER BY id DESC",
        "SELECT * FROM generate_series(1, 10) AS g",
        "SELECT * FROM orders AS o, LATERAL (SELECT 1) AS x",
    ],
)
def test_accepts(sql: str) -> None:
    assert guard(sql).endswith("LIMIT 500")


def test_case_insensitive_known_table_match() -> None:
    assert guard("SELECT * FROM ORDERS").endswith("LIMIT 500")


def test_cte_alias_is_not_treated_as_an_unknown_table() -> None:
    out = guard("WITH secrets_free AS (SELECT * FROM orders) SELECT * FROM secrets_free")
    assert out.endswith("LIMIT 500")


# --------------------------------------------------------------------------
# LIMIT policy
# --------------------------------------------------------------------------


def test_injects_limit_when_absent() -> None:
    assert guard("SELECT * FROM orders") == "SELECT * FROM orders LIMIT 500"


def test_leaves_a_smaller_limit_alone() -> None:
    assert guard("SELECT * FROM orders LIMIT 10") == "SELECT * FROM orders LIMIT 10"


def test_leaves_an_equal_limit_alone() -> None:
    assert guard("SELECT * FROM orders LIMIT 500") == "SELECT * FROM orders LIMIT 500"


def test_clamps_a_larger_limit() -> None:
    assert guard("SELECT * FROM orders LIMIT 9999") == "SELECT * FROM orders LIMIT 500"


def test_clamps_a_non_literal_limit() -> None:
    """`LIMIT ALL` / `LIMIT $1` cannot be bounded statically, so fail safe."""
    assert guard("SELECT * FROM orders LIMIT ALL") == "SELECT * FROM orders LIMIT 500"
    assert guard("SELECT * FROM orders LIMIT $1") == "SELECT * FROM orders LIMIT 500"


def test_preserves_offset_when_clamping() -> None:
    out = guard("SELECT * FROM orders LIMIT 9999 OFFSET 10")
    assert out == "SELECT * FROM orders LIMIT 500 OFFSET 10"


def test_limit_applies_to_the_outer_query_of_a_union() -> None:
    out = guard("SELECT id FROM orders UNION SELECT id FROM customers")
    assert out == "SELECT id FROM orders UNION SELECT id FROM customers LIMIT 500"


def test_limit_clamped_on_a_union() -> None:
    out = guard("SELECT id FROM orders UNION SELECT id FROM customers LIMIT 9999")
    assert out == "SELECT id FROM orders UNION SELECT id FROM customers LIMIT 500"


def test_inner_subquery_limit_is_untouched() -> None:
    out = guard("SELECT * FROM (SELECT * FROM orders LIMIT 3) AS t")
    assert out == "SELECT * FROM (SELECT * FROM orders LIMIT 3) AS t LIMIT 500"


def test_max_rows_is_honoured() -> None:
    assert guard("SELECT * FROM orders", max_rows=7) == "SELECT * FROM orders LIMIT 7"
    assert guard("SELECT * FROM orders LIMIT 50", max_rows=7) == "SELECT * FROM orders LIMIT 7"


def test_output_is_postgres_dialect_and_reparseable() -> None:
    out = guard("SELECT o.id FROM orders AS o JOIN customers AS c ON c.id = o.customer_id")
    # Guarding the guard's own output must be a fixed point.
    assert guard(out) == out
