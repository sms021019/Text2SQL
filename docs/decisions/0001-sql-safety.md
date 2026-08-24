# 0001. SQL safety: AST policy via sqlglot, plus DB-level defence in depth

## Status

Accepted — implemented in `backend/app/core/sql/guard.py` and
`backend/app/core/sql/executor.py`.

## Context

Generated SQL comes from an LLM and is executed against a real Postgres
database. It must never be able to write data, exfiltrate data outside the
exposed schema, or tie up the database indefinitely. A naive approach —
string-matching for `DROP`/`DELETE`/etc. — is easy to defeat (comments,
string literals containing keywords, `COPY ... TO PROGRAM`, function-based
side effects like `pg_sleep`) and easy to over-block (a column literally
named `update_count`). We need something precise enough to give a useful
rejection reason, cheap enough to run on every request, and backed by a
guarantee that doesn't depend on the parser being perfect.

## Decision

Two layers:

1. **`guard_sql()`** parses the candidate SQL with `sqlglot`'s Postgres
   dialect and walks the resulting AST. A statement is accepted only if:
   - it parses to exactly one statement (a trailing `;` is fine, and
     sqlglot's `exp.Command` fallback for unparseable syntax — `EXPLAIN`,
     `VACUUM`, etc. — is always rejected as unanalysable);
   - the root is a `SELECT`, or a `WITH ... SELECT`, or a set operation
     (`UNION`/`INTERSECT`/`EXCEPT`) whose every arm is itself a query;
   - no CTE body is a write and no DML/DDL node (`Insert`, `Update`,
     `Delete`, `Drop`, `Alter`, `TruncateTable`, `Set`, ...) appears
     anywhere in the tree;
   - there's no `SELECT ... INTO` and no row lock (`FOR UPDATE`/`FOR SHARE`);
   - no call to a denylisted function — matched both by an exact-name set
     (`pg_sleep`, `dblink`, `nextval`/`setval`, `pg_read_file`,
     `set_config`, ...) and by prefix families (`dblink*`, `lo_*`,
     `pg_advisory*`, `pg_terminate*`, ...) so an unenumerated variant of a
     known-bad family fails closed instead of slipping through;
   - every referenced table either is a CTE alias visible in that table's
     own lexical scope, or is a member of `known_tables` — the set of table
     names the pipeline actually retrieved for this question (see ADR
     0002), not the whole schema. A schema- or catalog-qualified name can
     never resolve to a CTE, only to a real table, so it always falls
     through to the known-table check.

   On acceptance, the statement is re-rendered with a `LIMIT` injected (if
   absent) or clamped down to `max_rows` on the *outermost* query only —
   inner subquery limits are left alone, since they're part of the query's
   meaning, not its blast radius.

2. **`execute_readonly()`** then runs the guarded SQL on a dedicated
   connection: it issues `SET TRANSACTION READ ONLY` and
   `SET LOCAL statement_timeout = <ms>` itself (not relying on the
   connecting role's own server-side defaults, since this executor may in
   principle be pointed at a role other than the project's own `readonly`
   role), fetches at most `max_rows + 1` rows to detect truncation without
   buffering an unbounded result, and always rolls the transaction back —
   it never commits. Separately, `seed/roles.sql` creates a Postgres
   `readonly` role with no write grants on `target_db`, and the backend's
   `TARGET_DB_URL` always connects as that role.

## Consequences

- **The AST gate is for UX and cheap rejection, not the security
  boundary.** `sqlglot`'s Postgres dialect isn't 100% faithful to real
  Postgres — some valid Postgres it can't parse (handled: reject, fail
  closed, as unanalysable) and in principle some it could parse
  differently than the server would. The denylist of dangerous functions
  is inherently incomplete no matter how many prefix families are added.
  None of that matters for the actual guarantee: the `readonly` role plus
  `statement_timeout` plus the row cap on the fetched result is what
  actually makes a malicious or malformed statement harmless. Treat any
  guard bypass as a UX bug (a bad error message, or a query that should
  have been rejected earlier), not a breach.
- Parsing cost is a few milliseconds per query — negligible next to LLM
  latency.
- Rejections are precise and explainable. `PipelineOutput.error` renders a
  `GuardError` as `f"guard:{reason}: {detail}"` — e.g.
  `guard:unknown_table: unknown table: secrets` or
  `guard:forbidden_function: function pg_sleep() is not allowed` — rather
  than an opaque database permission error, which is better UX and also
  gives a caller a stable `reason` prefix (`guard:not_select`,
  `guard:unknown_table`, `guard:forbidden_function`, ...) to key off of
  without depending on the human-readable detail — in particular, to
  *never* trigger the one-shot repair loop (ADR-adjacent: see
  `pipeline.py`'s `_REPAIRABLE_KINDS`). A rejected statement is terminal;
  the pipeline does not ask the LLM to "fix" a `DELETE`.
- `known_tables` being the *retrieved* set rather than the whole schema
  means a change to retrieval (top-k, hop depth) can, as a side effect,
  change which tables a query is allowed to reference — this is
  intentional (it keeps the guard's notion of "known" in sync with what
  was actually shown to the model) but worth knowing when debugging an
  `unknown_table` rejection that looks surprising.
