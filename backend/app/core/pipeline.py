"""End-to-end orchestration of the Text2SQL pipeline.

`Text2SQLPipeline.run()` wires together schema retrieval, prompt
construction, LLM generation, SQL guarding, and read-only execution:

    retrieve -> render -> generate -> parse -> guard -> execute

On an `ExecutionError` whose `kind` looks recoverable (a bad first-draft
query -- typically a syntax mistake or a reference to something that does
not exist) it asks the LLM once for a repair and retries guard + execute.
Every other failure -- an LLM transport error, a policy violation (a write,
an unknown table, ...), an unparsable response, a timeout/permission error,
or a second failed execution -- is terminal: `run()` never raises for these,
it reports them via `PipelineOutput.error` instead, as a stable
`"<stage>: <message>"` string -- `f"llm: {exc}"`, `f"parse: {exc}"`,
`f"execution:{exc.kind}: {exc.pg_message}"`, or, for a guard rejection,
`f"guard:{exc.reason}: {exc.detail}"` (e.g. `"guard:not_select: expected a
SELECT, found DELETE"`) -- so a caller can match on the stage/reason prefix
without depending on the human-readable detail. A `GuardError` is always
terminal, deliberately: the pipeline does not ask the LLM to "fix" a
`DELETE`. Unexpected exceptions (bugs) are not caught here and propagate.

Two-tier cache (optional, `cache=` constructor arg)
----------------------------------------------------
When a `SqlCache` is supplied and `run(..., use_cache=True)` (the default),
`run()` derives a cache key via `cache.key_for(question)` and tries, in
order:

1. The **result** tier (opt-in via `use_result_cache=True`): a hit skips
   retrieval, the LLM, and the database entirely, returning a complete
   `PipelineOutput` built from the cached SQL/explanation/tables/result,
   `timings=[("result_cache", ms)]`, zeroed `usage`, and
   `cache_status="result_hit"`.
2. The **SQL** tier: a hit skips retrieve/render/generate/parse but still
   re-runs the guard (cheap defence-in-depth against a cache entry that
   predates a guard policy change) and execution, `cache_status="sql_hit"`.
   A guard rejection evicts the entry (`cache.delete_sql(key)`) and returns
   the guard error directly -- no fallthrough, since a policy rejection is
   about the SQL text itself, not a stale-schema mismatch a regeneration
   would fix, so nothing is gained by burning a fresh LLM call on the same
   question; evicting still matters, so a tightened guard policy does not
   pin a now-forbidden statement in the cache for its full TTL. An
   execution failure is handled more narrowly: only when
   `ExecutionError.kind` is one `_repair()` would itself have retried for a
   freshly-generated query (`_REPAIRABLE_KINDS` -- `"syntax"`/`"other"`,
   `_should_evict()`) is the entry evicted and the question falls through to
   the full retrieve-through-execute path below, self-healing a cache
   poisoned by, e.g., a schema change that outdated the cached SQL -- the
   eventual response's `cache_status` reflects that fallthrough (`"miss"`),
   not `"sql_hit"`. A `"timeout"` or `"permission"` failure is an
   environmental problem a fresh generation would hit identically (same
   statement timeout, same read-only role), so it is terminal instead: the
   entry is left cached and `cache_status` stays `"sql_hit"`.
3. A **miss** on both runs the full path described above. On a successful
   execution the (possibly repaired) SQL is written back via
   `cache.set_sql()`, and, if `use_result_cache=True`, the result via
   `cache.set_result()`. A failed run is never cached, at any tier.

`cache_status` on the returned `PipelineOutput` is `"disabled"` when no
cache was supplied at all, `"bypass"` when `use_cache=False` (the cache is
not read *or* written for that call), and otherwise one of `"miss"`,
`"sql_hit"`, `"result_hit"` as above.

Observer hooks
---------------
Every stage timing, LLM completion, guard rejection, execution outcome,
cache read attempt, and the run as a whole is reported to `observer` (an
`app.core.observer.PipelineObserver`; defaults to a no-op `NullObserver`)
so a caller can turn this into metrics/logs without `app/core/**` knowing
anything about *how*.

`app/core/**` must never import fastapi/redis/app.api (see
`app/core/errors.py`), so this module only depends on the plain-Python
layers below it plus SQLAlchemy's `AsyncEngine` type. `SqlCache` is defined
here (rather than imported from `app.cache`) precisely to keep that true --
it is a structural `Protocol` that `app.cache.query_cache.QueryCache`
happens to satisfy, not a shared base class.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.core.errors import LLMError
from app.core.observer import NullObserver, PipelineObserver
from app.core.prompting.builder import PromptBuilder, PromptParseError, parse_llm_output
from app.core.schema.models import SchemaGraph
from app.core.schema.render import render_ddl
from app.core.schema.retrieve import SchemaRetriever
from app.core.sql.executor import ExecutionError, QueryResult, execute_readonly
from app.core.sql.guard import GuardError, guard_sql
from app.llm.base import Completion, LLMClient, Usage

__all__ = ["CacheStatus", "PipelineOutput", "SqlCache", "StageTiming", "Text2SQLPipeline"]

logger = logging.getLogger(__name__)

#: `ExecutionError.kind`s worth a single repair attempt. `timeout` and
#: `permission` are excluded on purpose: retrying the same statement against
#: the same read-only role and the same statement timeout would just fail
#: the same way again, so those are terminal instead.
_REPAIRABLE_KINDS = frozenset({"syntax", "other"})

CacheStatus = Literal["miss", "sql_hit", "result_hit", "bypass", "disabled"]


class SqlCache(Protocol):
    """Duck-typed two-tier cache the pipeline can use, keyed by a single
    `key_for(question)` digest shared by both tiers (mirroring
    `app.cache.keys.sql_cache_key`/`result_cache_key`). Implemented by
    `app.cache.query_cache.QueryCache`.

    The SQL tier stores the guard-passed (and, if applicable, repaired) SQL
    plus its explanation and the table names it referenced -- the latter so
    a SQL-tier hit can re-run the guard without re-retrieving them. The
    result tier additionally stores a full `QueryResult`, so a result-tier
    hit can answer a question without touching the database at all.
    """

    def key_for(self, question: str) -> str: ...

    async def get_sql(self, key: str) -> tuple[str, str, list[str]] | None:
        """`(sql, explanation, tables)` on a hit, `None` on a miss."""
        ...

    async def set_sql(self, key: str, sql: str, explanation: str, tables: list[str]) -> None: ...

    async def delete_sql(self, key: str) -> None: ...

    async def get_result(self, key: str) -> tuple[str, str, list[str], QueryResult] | None:
        """`(sql, explanation, tables, result)` on a hit, `None` on a miss."""
        ...

    async def set_result(
        self, key: str, sql: str, explanation: str, tables: list[str], result: QueryResult
    ) -> None: ...


@dataclass(frozen=True)
class StageTiming:
    stage: str
    ms: float


@dataclass(frozen=True)
class PipelineOutput:
    sql: str
    explanation: str
    result: QueryResult | None
    tables: list[str]
    repaired: bool
    usage: Usage
    timings: list[StageTiming]
    error: str | None
    model: str
    cache_status: CacheStatus


@contextmanager
def _timed(timings: list[StageTiming], stage: str, observer: PipelineObserver) -> Iterator[None]:
    """Time one stage, appending a `StageTiming` and logging it on exit.

    Runs on both success and failure -- a stage that raises still gets a
    timing entry, a log line, and an `observer.on_stage()` call for how long
    it ran before failing.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - start) * 1000
        timings.append(StageTiming(stage=stage, ms=ms))
        logger.info("stage", extra={"stage": stage, "ms": ms})
        observer.on_stage(stage, ms)


class _UsageAccumulator:
    """Sums `Usage` across the generate call and, if it happens, the repair
    call, and tracks the model name of the most recent completion."""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.latency_ms = 0.0
        self.model = ""

    def add(self, completion: Completion) -> None:
        self.prompt_tokens += completion.usage.prompt_tokens
        self.completion_tokens += completion.usage.completion_tokens
        self.latency_ms += completion.usage.latency_ms
        self.model = completion.model

    def usage(self) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            latency_ms=self.latency_ms,
        )


def _execution_outcome(exc: ExecutionError) -> str:
    """Map an `ExecutionError.kind` onto the coarser
    `PipelineObserver.on_execution` outcome vocabulary (`success` is
    reported separately by callers on the non-error path)."""
    return "timeout" if exc.kind == "timeout" else "db_error"


def _should_evict(exc: ExecutionError) -> bool:
    """Whether a SQL-tier cache hit's execution failure means the cached
    statement itself is stale and worth evicting + regenerating (see
    `Text2SQLPipeline._sql_hit`).

    Uses the exact same `_REPAIRABLE_KINDS` test `_full_path`/`_repair` use
    to decide whether a *freshly generated* query is worth one repair
    attempt: `"syntax"`/`"other"` plausibly means the statement no longer
    matches the current schema (evict, regenerate); `"timeout"`/
    `"permission"` is an environmental failure a fresh generation would hit
    identically, so the cached entry is not to blame and stays cached.
    """
    return exc.kind in _REPAIRABLE_KINDS


class Text2SQLPipeline:
    """Runs one natural-language question through schema retrieval, LLM
    generation, guarding, and read-only execution -- with a single repair
    attempt on a recoverable execution failure, an optional two-tier cache
    short-circuiting some or all of that, and observer hooks throughout.
    See the module docstring for the cache and observer semantics."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        retriever: SchemaRetriever,
        graph: SchemaGraph,
        builder: PromptBuilder,
        target_engine: AsyncEngine,
        settings: Settings,
        observer: PipelineObserver | None = None,
        cache: SqlCache | None = None,
    ) -> None:
        self._llm = llm
        self._retriever = retriever
        self._graph = graph
        self._builder = builder
        self._target_engine = target_engine
        self._settings = settings
        self._observer: PipelineObserver = observer if observer is not None else NullObserver()
        self._cache = cache

    async def run(
        self, question: str, *, use_cache: bool = True, use_result_cache: bool = False
    ) -> PipelineOutput:
        start = time.perf_counter()
        ok = False
        try:
            out = await self._run(question, use_cache=use_cache, use_result_cache=use_result_cache)
            ok = out.error is None
            return out
        finally:
            ms = (time.perf_counter() - start) * 1000
            self._observer.on_pipeline_done(ms=ms, ok=ok)

    async def _run(
        self, question: str, *, use_cache: bool, use_result_cache: bool
    ) -> PipelineOutput:
        if self._cache is None:
            return await self._full_path(question, cache_status="disabled")
        cache = self._cache
        if not use_cache:
            self._observer.on_cache(cache="result", outcome="bypass")
            self._observer.on_cache(cache="sql", outcome="bypass")
            return await self._full_path(question, cache_status="bypass")

        key = cache.key_for(question)

        if use_result_cache:
            start = time.perf_counter()
            cached_result = await cache.get_result(key)
            ms = (time.perf_counter() - start) * 1000
            if cached_result is not None:
                self._observer.on_cache(cache="result", outcome="hit")
                self._observer.on_stage("result_cache", ms)
                sql, explanation, tables, result = cached_result
                return self._result_hit(sql, explanation, tables, result, ms)
            self._observer.on_cache(cache="result", outcome="miss")
        else:
            self._observer.on_cache(cache="result", outcome="bypass")

        cached_sql = await cache.get_sql(key)
        if cached_sql is None:
            self._observer.on_cache(cache="sql", outcome="miss")
            return await self._full_path(
                question, cache_status="miss", cache_key=key, use_result_cache=use_result_cache
            )

        self._observer.on_cache(cache="sql", outcome="hit")
        sql, explanation, tables = cached_sql
        return await self._sql_hit(
            question, cache, key, sql, explanation, tables, use_result_cache=use_result_cache
        )

    def _result_hit(
        self, sql: str, explanation: str, tables: list[str], result: QueryResult, ms: float
    ) -> PipelineOutput:
        return PipelineOutput(
            sql=sql,
            explanation=explanation,
            result=result,
            tables=tables,
            repaired=False,
            usage=Usage(prompt_tokens=0, completion_tokens=0, latency_ms=0.0),
            timings=[StageTiming(stage="result_cache", ms=ms)],
            error=None,
            model=self._settings.llm_model,
            cache_status="result_hit",
        )

    async def _sql_hit(
        self,
        question: str,
        cache: SqlCache,
        key: str,
        sql: str,
        explanation: str,
        tables: list[str],
        *,
        use_result_cache: bool,
    ) -> PipelineOutput:
        """Skip retrieve/render/generate/parse for a cached SQL statement,
        but still guard and execute it -- see the module docstring's
        "Two-tier cache" section for the self-healing fallthrough on a
        failed execution."""
        timings: list[StageTiming] = []
        usage = _UsageAccumulator()

        try:
            with _timed(timings, "guard", self._observer):
                guarded_sql = guard_sql(sql, set(tables), max_rows=self._settings.max_rows)
        except GuardError as exc:
            self._observer.on_guard_reject(exc.reason)
            self._observer.on_execution(outcome="rejected", ms=0.0)
            # Evict even though there is no fallthrough: a guard policy that
            # tightened since this entry was cached should not keep pinning
            # a now-forbidden statement for the rest of its TTL.
            await cache.delete_sql(key)
            return self._finish(
                sql=sql,
                explanation=explanation,
                result=None,
                tables=tables,
                repaired=False,
                usage=usage,
                timings=timings,
                error=f"guard:{exc.reason}: {exc.detail}",
                cache_status="sql_hit",
            )

        try:
            with _timed(timings, "execute", self._observer):
                result = await execute_readonly(
                    self._target_engine,
                    guarded_sql,
                    statement_timeout_ms=self._settings.statement_timeout_ms,
                    max_rows=self._settings.max_rows,
                )
        except ExecutionError as exc:
            self._observer.on_execution(outcome=_execution_outcome(exc), ms=timings[-1].ms)
            if not _should_evict(exc):
                # Environmental failure (timeout/permission): a fresh
                # generation would hit the same wall, so the cached entry is
                # not at fault -- leave it cached and report the error
                # directly rather than burning an LLM call that cannot help.
                return self._finish(
                    sql=guarded_sql,
                    explanation=explanation,
                    result=None,
                    tables=tables,
                    repaired=False,
                    usage=usage,
                    timings=timings,
                    error=f"execution:{exc.kind}: {exc.pg_message}",
                    cache_status="sql_hit",
                )
            await cache.delete_sql(key)
            return await self._full_path(
                question, cache_status="miss", cache_key=key, use_result_cache=use_result_cache
            )

        self._observer.on_execution(outcome="success", ms=timings[-1].ms)
        if use_result_cache:
            await cache.set_result(key, guarded_sql, explanation, tables, result)

        return self._finish(
            sql=guarded_sql,
            explanation=explanation,
            result=result,
            tables=tables,
            repaired=False,
            usage=usage,
            timings=timings,
            error=None,
            cache_status="sql_hit",
        )

    async def _full_path(
        self,
        question: str,
        *,
        cache_status: CacheStatus,
        cache_key: str | None = None,
        use_result_cache: bool = False,
    ) -> PipelineOutput:
        timings: list[StageTiming] = []
        usage = _UsageAccumulator()
        tables: list[str] = []
        sql = ""
        explanation = ""

        try:
            with _timed(timings, "retrieve", self._observer):
                tables = await self._retriever.retrieve(question)

            with _timed(timings, "render", self._observer):
                ddl = render_ddl(self._graph, tables)

            prompt = self._builder.generate(question, ddl)
            with _timed(timings, "generate", self._observer):
                completion = await self._llm.complete(prompt.system, prompt.user)
            usage.add(completion)
            self._observer.on_llm(stage="generate", model=completion.model, usage=completion.usage)

            with _timed(timings, "parse", self._observer):
                sql, explanation = parse_llm_output(completion.text)

            with _timed(timings, "guard", self._observer):
                guarded_sql = guard_sql(sql, set(tables), max_rows=self._settings.max_rows)
            sql = guarded_sql

            try:
                with _timed(timings, "execute", self._observer):
                    result = await execute_readonly(
                        self._target_engine,
                        guarded_sql,
                        statement_timeout_ms=self._settings.statement_timeout_ms,
                        max_rows=self._settings.max_rows,
                    )
            except ExecutionError as exc:
                self._observer.on_execution(outcome=_execution_outcome(exc), ms=timings[-1].ms)
                if exc.kind not in _REPAIRABLE_KINDS:
                    return self._finish(
                        sql=sql,
                        explanation=explanation,
                        result=None,
                        tables=tables,
                        repaired=False,
                        usage=usage,
                        timings=timings,
                        error=f"execution:{exc.kind}: {exc.pg_message}",
                        cache_status=cache_status,
                    )
                return await self._repair(
                    question,
                    ddl,
                    guarded_sql,
                    exc,
                    tables,
                    usage,
                    timings,
                    cache_status,
                    cache_key,
                    use_result_cache,
                )

            self._observer.on_execution(outcome="success", ms=timings[-1].ms)
            await self._maybe_cache_write(
                cache_key, sql, explanation, tables, result, use_result_cache
            )

            return self._finish(
                sql=sql,
                explanation=explanation,
                result=result,
                tables=tables,
                repaired=False,
                usage=usage,
                timings=timings,
                error=None,
                cache_status=cache_status,
            )

        except LLMError as exc:
            return self._finish(
                sql=sql,
                explanation=explanation,
                result=None,
                tables=tables,
                repaired=False,
                usage=usage,
                timings=timings,
                error=f"llm: {exc}",
                cache_status=cache_status,
            )
        except PromptParseError as exc:
            return self._finish(
                sql="",
                explanation="",
                result=None,
                tables=tables,
                repaired=False,
                usage=usage,
                timings=timings,
                error=f"parse: {exc}",
                cache_status=cache_status,
            )
        except GuardError as exc:
            self._observer.on_guard_reject(exc.reason)
            self._observer.on_execution(outcome="rejected", ms=0.0)
            return self._finish(
                sql=sql,
                explanation=explanation,
                result=None,
                tables=tables,
                repaired=False,
                usage=usage,
                timings=timings,
                error=f"guard:{exc.reason}: {exc.detail}",
                cache_status=cache_status,
            )

    async def _repair(
        self,
        question: str,
        ddl: str,
        bad_sql: str,
        exc: ExecutionError,
        tables: list[str],
        usage: _UsageAccumulator,
        timings: list[StageTiming],
        cache_status: CacheStatus,
        cache_key: str | None,
        use_result_cache: bool,
    ) -> PipelineOutput:
        """One repair attempt: a second LLM call, parse, and guard, all
        timed as a single `repair` stage; execution (if reached) gets its
        own `execute` timing entry, same as the first attempt."""
        start = time.perf_counter()
        sql = ""
        explanation = ""
        try:
            prompt = self._builder.repair(question, ddl, bad_sql, exc.pg_message)
            completion = await self._llm.complete(prompt.system, prompt.user)
            usage.add(completion)
            self._observer.on_llm(stage="repair", model=completion.model, usage=completion.usage)
            sql, explanation = parse_llm_output(completion.text)
            guarded_sql = guard_sql(sql, set(tables), max_rows=self._settings.max_rows)
        except LLMError as inner:
            self._log_repair_stage(timings, start)
            return self._finish(
                sql="",
                explanation="",
                result=None,
                tables=tables,
                repaired=True,
                usage=usage,
                timings=timings,
                error=f"llm: {inner}",
                cache_status=cache_status,
            )
        except PromptParseError as inner:
            self._log_repair_stage(timings, start)
            return self._finish(
                sql="",
                explanation="",
                result=None,
                tables=tables,
                repaired=True,
                usage=usage,
                timings=timings,
                error=f"parse: {inner}",
                cache_status=cache_status,
            )
        except GuardError as inner:
            self._log_repair_stage(timings, start)
            self._observer.on_guard_reject(inner.reason)
            self._observer.on_execution(outcome="rejected", ms=0.0)
            return self._finish(
                sql=sql,
                explanation=explanation,
                result=None,
                tables=tables,
                repaired=True,
                usage=usage,
                timings=timings,
                error=f"guard:{inner.reason}: {inner.detail}",
                cache_status=cache_status,
            )

        self._log_repair_stage(timings, start)

        try:
            with _timed(timings, "execute", self._observer):
                result = await execute_readonly(
                    self._target_engine,
                    guarded_sql,
                    statement_timeout_ms=self._settings.statement_timeout_ms,
                    max_rows=self._settings.max_rows,
                )
        except ExecutionError as inner:
            self._observer.on_execution(outcome=_execution_outcome(inner), ms=timings[-1].ms)
            return self._finish(
                sql=guarded_sql,
                explanation=explanation,
                result=None,
                tables=tables,
                repaired=True,
                usage=usage,
                timings=timings,
                error=f"execution:{inner.kind}: {inner.pg_message}",
                cache_status=cache_status,
            )

        self._observer.on_execution(outcome="success", ms=timings[-1].ms)
        await self._maybe_cache_write(
            cache_key, guarded_sql, explanation, tables, result, use_result_cache
        )

        return self._finish(
            sql=guarded_sql,
            explanation=explanation,
            result=result,
            tables=tables,
            repaired=True,
            usage=usage,
            timings=timings,
            error=None,
            cache_status=cache_status,
        )

    async def _maybe_cache_write(
        self,
        cache_key: str | None,
        sql: str,
        explanation: str,
        tables: list[str],
        result: QueryResult,
        use_result_cache: bool,
    ) -> None:
        """Write-back after a successful execution on the full path -- a
        no-op unless this run had a cache key (i.e. it was a cache miss with
        caching enabled; `cache_status in {"disabled", "bypass"}` never
        passes a `cache_key`, and a repair-path fallthrough from a stale
        SQL-tier hit reuses the same key it evicted)."""
        if cache_key is None or self._cache is None:
            return
        cache = self._cache
        await cache.set_sql(cache_key, sql, explanation, tables)
        if use_result_cache:
            await cache.set_result(cache_key, sql, explanation, tables, result)

    def _log_repair_stage(self, timings: list[StageTiming], start: float) -> None:
        ms = (time.perf_counter() - start) * 1000
        timings.append(StageTiming(stage="repair", ms=ms))
        logger.info("stage", extra={"stage": "repair", "ms": ms})
        self._observer.on_stage("repair", ms)

    @staticmethod
    def _finish(
        *,
        sql: str,
        explanation: str,
        result: QueryResult | None,
        tables: list[str],
        repaired: bool,
        usage: _UsageAccumulator,
        timings: list[StageTiming],
        error: str | None,
        cache_status: CacheStatus,
    ) -> PipelineOutput:
        return PipelineOutput(
            sql=sql,
            explanation=explanation,
            result=result,
            tables=tables,
            repaired=repaired,
            usage=usage.usage(),
            timings=list(timings),
            error=error,
            model=usage.model,
            cache_status=cache_status,
        )
