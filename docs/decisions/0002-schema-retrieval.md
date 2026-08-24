# 0002. Schema retrieval: table embeddings + keyword boost + FK-hop expansion

## Status

Accepted — implemented in `backend/app/core/schema/retrieve.py`.

## Context

The full NorthwindNext schema (11 tables) is small enough to dump into
every prompt, but that doesn't scale and doesn't demonstrate anything about
retrieval quality. We need a way to pick a relevant subset of tables for a
given question, cheaply, without standing up a graph database or an extra
LLM call — and the pick needs to include the tables required for joins,
not just the ones the question names directly.

## Decision

`SchemaRetriever` scores every table for a question and returns a ranked
subset:

1. **Embed once per table**, not per column. Each table's
   `summary()` (name + comment + column names/types/comments) is embedded
   when the index is built — via `build_index()`, called from the FastAPI
   lifespan with a startup timeout — and cached in memory as a matrix.
   Rebuilding is a full re-embed (`POST /schema/refresh`), not incremental.
2. **Score = cosine similarity + a keyword boost.** The question is
   embedded per request and cosine-compared against every table vector.
   Separately, a flat `+0.2` is added for any table whose name (singular or
   plural) or any of its column names appears in the question as a whole
   word (case-insensitive regex on word boundaries) — this is the
   mitigation called out in PLAN.md D2 for the case embeddings alone tend
   to miss: a question that hinges on one specific column name (e.g.
   "customers with a `segment` of VIP") ranks correctly even if the
   semantic embedding similarity alone wouldn't have surfaced it.
3. **Take the top-k** (`RETRIEVE_TOP_K`, default 4) by combined score,
   ties broken by table name for determinism.
4. **Expand by FK hops** (`hops`, default 1): starting from the top-k,
   pull in every table reachable via a foreign-key edge in the introspected
   `SchemaGraph`, so a question that names one table but needs a join
   partner (e.g. "products" needing `categories` for a category name) still
   gets a schema excerpt that supports the join. The returned list is
   `top-k tables, then FK neighbours`, in that order.
5. **Lazy self-heal.** If the LLM was unreachable when the lifespan tried
   to build the index at startup (or a startup timeout cancelled the
   attempt), the retriever doesn't crash the app — it stashes the failure
   and retries `build_index()` on the *next* call to `retrieve()`/`scores()`,
   so a transient outage at boot doesn't permanently break retrieval for
   the process lifetime; it surfaces as an `llm:` pipeline error on
   whichever request first hits it while the LLM is still down.

The retrieved table list also becomes `known_tables` for the SQL guard
(ADR 0001) — the guard only accepts tables the retriever actually surfaced
for this question, not the whole schema.

## Alternatives considered

- **Dump the whole schema into every prompt.** Simplest option, and fine
  at 11 tables, but doesn't scale past a couple hundred and gives up any
  chance to measure or tune retrieval quality.
- **Column-level embeddings.** Finer-grained, but noisier and
  proportionally far more tokens/embeddings to maintain for marginal gain
  at this schema size.
- **LLM-driven schema linking** ("which tables are relevant to this
  question?" as its own model call). Likely better on genuinely ambiguous
  questions, at the cost of an extra LLM round trip (latency and money)
  per query.
- **A graph database (e.g. Neo4j) for schema traversal.** Overkill for 11
  tables and 10 FK edges; the FK-hop expansion here is the same underlying
  idea done in-process against the already-introspected `SchemaGraph`.

## Consequences

- Table-level embeddings can still miss a question that hinges on a
  single, unusually-named column with no keyword overlap and weak semantic
  similarity to the table's overall summary — the keyword boost narrows
  this gap for exact/near-exact mentions but doesn't eliminate it for
  paraphrases.
- `top_k` and `hops` are both plain constructor parameters
  (`RETRIEVE_TOP_K` env var for `top_k`), so retrieval behavior is easy to
  tune, and `scripts/eval.py`'s row-match metric against
  `seed/questions.yaml` is the intended way to quantify the effect of
  changing either one — retrieval-quality changes become a number, not a
  guess.
- Embeddings live in an in-process `numpy` array, rebuilt from scratch on
  every process start and on `/schema/refresh`. This is deliberately
  simple for Phase 1; Phase 2 moves the cache into Redis (keyed by a schema
  version hash) so a restart doesn't force a full re-embed and so multiple
  backend replicas can share one index instead of each building — and
  paying for — their own.
- Because `known_tables` for the guard is exactly the retrieved set,
  retrieval misses don't just produce a worse prompt — they can also
  produce an `unknown_table` guard rejection for a query that would
  otherwise have been valid, if the LLM correctly infers it needs a table
  that wasn't retrieved. This coupling is intentional (see ADR 0001) but is
  a second reason retrieval quality is worth measuring, not just answer
  quality.
