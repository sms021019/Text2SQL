"""Pure functions deriving cache keys.

These produce the *unprefixed* logical keys (`sql:<hash>`, `res:<hash>`,
`schema:<version>`); `RedisCache` (see `app/cache/redis.py`) adds the
`{namespace}:` prefix on top when it talks to Redis. Kept dependency-free
(no `redis` import) so they're trivially unit-testable.
"""

from __future__ import annotations

import hashlib

__all__ = [
    "SCHEMA_EPOCH_KEY",
    "normalise_question",
    "result_cache_key",
    "schema_cache_key",
    "sql_cache_key",
]

_TRAILING_PUNCT = "?.!;"

#: The cross-process schema-refresh token (`app.services.schema_sync`).
#: A constant rather than a function -- there is exactly one, shared by
#: every process on this Redis, and it is deliberately *not* keyed by
#: schema version: it is what tells a process its version may be stale.
#: It shares the `schema:` prefix that `SchemaCache.invalidate_all()`
#: sweeps, which is why that method preserves this key explicitly.
SCHEMA_EPOCH_KEY = "schema:epoch"


def normalise_question(q: str) -> str:
    """Lowercase, collapse all internal whitespace runs to single spaces,
    and strip trailing punctuation (`?.!;`) plus any surrounding
    whitespace."""
    collapsed = " ".join(q.lower().split())
    return collapsed.rstrip(_TRAILING_PUNCT).strip()


def sql_cache_key(*, schema_version: str, model: str, prompt_version: str, question: str) -> str:
    """`sql:{sha256(schema_version|model|prompt_version|normalised_question)[:32]}`."""
    normalised = normalise_question(question)
    payload = "|".join([schema_version, model, prompt_version, normalised])
    digest = hashlib.sha256(payload.encode()).hexdigest()[:32]
    return f"sql:{digest}"


def result_cache_key(*, sql_key: str) -> str:
    """`res:{same hash as sql_key}` -- results are keyed by the same digest
    as the SQL they came from, so invalidating one query invalidates both."""
    digest = sql_key.removeprefix("sql:")
    return f"res:{digest}"


def schema_cache_key(version: str) -> str:
    return f"schema:{version}"
