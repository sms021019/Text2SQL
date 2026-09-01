# 0003. Cache design: two tiers in Redis, keyed by everything that changes the answer

## Status

Accepted — implemented in `backend/app/cache/**` (keys, Redis wrapper,
`QueryCache`, `SchemaCache`) and the tier logic in
`backend/app/core/pipeline.py`.

## Context

The LLM call dominates both latency (seconds) and cost (tokens) of a
request; everything else in the pipeline is milliseconds. Repeated
questions are the norm in a demo, an eval run, and a load test alike, so
caching is the single largest available win. Three things make it awkward:

- **The answer depends on more than the question.** A different schema
  version, model, or prompt template can legitimately produce different
  SQL for the same words.
- **Cached rows go stale.** The target database is live; the cache returns
  data, not just a query.
- **Redis must not become a hard dependency.** A cache outage may cost
  latency, never availability.

Separately, rebuilding the retrieval embedding index at every process start
means re-embedding every table summary through the LLM — slow, and paid for
again by every replica.

## Decision

**One key derivation, two tiers, plus a schema tier.**

1. **Key** (`app/cache/keys.py`):
   `sha256(schema_version | model | prompt_version | normalised_question)`,
   truncated to 32 hex chars, prefixed `sql:` for the SQL tier and `res:`
   for the result tier — the *same* digest, so one question's two entries
   invalidate together. `RedisCache` adds a `t2s:` namespace on top.
   Normalisation is deliberately conservative: lowercase, collapse internal
   whitespace, strip trailing `?.!;`. Because the schema version, model, and
   prompt version are *in* the key, changing any of them produces misses on
   its own — there is no invalidation storm to orchestrate.

2. **SQL tier** (long TTL, `SQL_CACHE_TTL_S`, default 24 h, on by default).
   Stores the guard-passed (possibly repaired) SQL and the tables it
   referenced. A hit skips retrieve/render/generate/parse — the expensive
   LLM half — but still re-runs the guard and always re-executes against
   Postgres, so the rows are fresh and a cache entry that predates a guard
   policy change cannot slip through.

3. **Result tier** (short TTL, `RESULT_CACHE_TTL_S`, default 5 min, opt-in
   per request via `use_result_cache`). Additionally stores the serialised
   `QueryResult`; a hit skips the database too. Opt-in because it is the
   only tier that can hand back stale rows. A serialised entry over
   `RESULT_CACHE_MAX_BYTES` (1 MiB) is not stored at all, so one large row
   set cannot evict everything else under memory pressure.

4. **Failures are never cached, at any tier.** On a SQL-tier hit that then
   fails, the pipeline distinguishes two cases: a guard rejection or a
   *repairable* execution error (`syntax`/`other`) evicts the entry — the
   former terminally (regenerating the same question would not un-reject
   it), the latter falling through to the full path, self-healing an entry
   outdated by a schema change. A `timeout` or `permission` failure is
   environmental — a fresh generation would hit the identical statement
   timeout and read-only role — so the entry is left in place and the error
   is returned.

5. **Schema tier** (`SchemaCache`, `SCHEMA_CACHE_TTL_S`, default 7 days).
   The introspected `SchemaGraph`, the table-name list, and the embeddings
   matrix are stored under `schema:{version}`, so a restart — or a second
   replica — loads the index instead of re-embedding it. All three parts
   must be present and the stored graph's version must match, or it is a
   miss.

6. **Fail-open.** `RedisCache` swallows `RedisError`/`OSError` in every
   method: an outage reads as a miss and writes as a no-op, and the API
   keeps answering. It is also the only module allowed to import `redis` —
   `app/core/**` sees a duck-typed `SqlCache` `Protocol` instead.

7. **The outcome is visible.** Every response carries
   `cache_status: miss | sql_hit | result_hit | bypass | disabled`
   (`bypass` = `use_cache=false`, `disabled` = no cache configured), the
   frontend renders it as a badge, and every read attempt increments
   `t2s_cache_requests_total{cache,outcome}`.

## Alternatives considered

- **Result cache only.** Simplest, but either the TTL is short (and the
  expensive LLM call is repaid constantly) or it is long (and the demo
  shows stale rows). The split lets the LLM result live for a day while the
  data stays as fresh as the request wants.
- **SQL cache only.** What the SQL tier already is; the result tier exists
  for the read-heavy, "same dashboard question every few seconds" case
  where re-executing is the remaining cost.
- **Semantic dedup** (treat two questions as one if their embeddings are
  close). Higher hit rate, but a false positive silently answers a
  different question than the one asked — an unacceptable failure mode for
  a system whose whole pitch is that its output is checkable.
- **Caching in Postgres** (a table in `app_db`, or pgvector). Avoids a new
  service, but the target DB is read-only by design and the app DB is for
  durable state, not for a TTL-driven cache; Redis is also what the Phase 4
  Kubernetes/cloud story wants anyway.

## Consequences

- Redis is a new operational dependency, but a soft one: with it down the
  service degrades to "every request is a miss" (plus `POST /query/async`
  returning 503 — see ADR 0004), not to an outage.
- **Cache stampede is accepted, not solved.** N concurrent identical
  questions all miss and all call the LLM. At this scale that costs a few
  duplicate completions; a short lock or single-flight is the obvious fix
  if it ever matters.
- The result tier can return rows up to its TTL out of date. That is why it
  is opt-in per request rather than a default.
- On the SQL-tier fallthrough path, `t2s_cache_requests_total` records
  `cache="sql", outcome="hit"` while the response reports
  `cache_status="miss"`. Both are correct — the cache *read* hit, the
  *response* did not come from the cache — but the two numbers will not
  agree exactly, and that is worth knowing before treating the metric as a
  response-level hit rate.
- **`query_log` has no `cache_status` column.** The per-request outcome is
  in the API response and in Prometheus, but it cannot be queried
  historically out of `app_db` — so "what fraction of last week's questions
  were cache hits" is not answerable from the database today. Adding the
  column (plus an Alembic migration) is a deliberate follow-up, not a gap
  in the cache itself.
- Key derivation is baked into `QueryCache` at construction, so a schema
  refresh or model change requires building a new `QueryCache` *and*
  `Text2SQLPipeline` — which is why both are built together in
  `app.services.schema_service.build_pipeline` and swapped together by
  `refresh_components`.
