# Architecture Decision Records

One file per decision, numbered in the order they were written. Format:
Status / Context / Decision / Consequences (and, where useful, Alternatives
considered). Source material for these is `PLAN.md` §4 ("Key design
decisions and their tradeoffs"), updated to match what was actually built.

| ADR | Title |
|---|---|
| [0001](0001-sql-safety.md) | SQL safety: AST policy via sqlglot, plus DB-level defence in depth |
| [0002](0002-schema-retrieval.md) | Schema retrieval: table embeddings + keyword boost + FK-hop expansion |
| [0003](0003-cache-design.md) | Cache design: two tiers in Redis, keyed by everything that changes the answer |
| [0004](0004-async-jobs.md) | Async execution: arq on Redis alongside the synchronous endpoint |
| [0005](0005-observability.md) | Observability: an observer hook in the core, Prometheus at the edge |

More ADRs land as later phases implement the remaining decisions in
`PLAN.md` §4 (the LLM provider abstraction, the self-correction loop, the
two-database/two-role split, and the Kubernetes/cloud footprint).
