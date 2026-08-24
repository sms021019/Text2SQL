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

`app/core/**` must never import fastapi/redis/app.api (see
`app/core/errors.py`), so this module only depends on the plain-Python
layers below it plus SQLAlchemy's `AsyncEngine` type.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.core.errors import LLMError
from app.core.prompting.builder import PromptBuilder, PromptParseError, parse_llm_output
from app.core.schema.models import SchemaGraph
from app.core.schema.render import render_ddl
from app.core.schema.retrieve import SchemaRetriever
from app.core.sql.executor import ExecutionError, QueryResult, execute_readonly
from app.core.sql.guard import GuardError, guard_sql
from app.llm.base import Completion, LLMClient, Usage

__all__ = ["PipelineOutput", "StageTiming", "Text2SQLPipeline"]

logger = logging.getLogger(__name__)

#: `ExecutionError.kind`s worth a single repair attempt. `timeout` and
#: `permission` are excluded on purpose: retrying the same statement against
#: the same read-only role and the same statement timeout would just fail
#: the same way again, so those are terminal instead.
_REPAIRABLE_KINDS = frozenset({"syntax", "other"})


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


@contextmanager
def _timed(timings: list[StageTiming], stage: str) -> Iterator[None]:
    """Time one stage, appending a `StageTiming` and logging it on exit.

    Runs on both success and failure -- a stage that raises still gets a
    timing entry and a log line for how long it ran before failing.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - start) * 1000
        timings.append(StageTiming(stage=stage, ms=ms))
        logger.info("stage", extra={"stage": stage, "ms": ms})


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


class Text2SQLPipeline:
    """Runs one natural-language question through schema retrieval, LLM
    generation, guarding, and read-only execution -- with a single repair
    attempt on a recoverable execution failure."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        retriever: SchemaRetriever,
        graph: SchemaGraph,
        builder: PromptBuilder,
        target_engine: AsyncEngine,
        settings: Settings,
    ) -> None:
        self._llm = llm
        self._retriever = retriever
        self._graph = graph
        self._builder = builder
        self._target_engine = target_engine
        self._settings = settings

    async def run(self, question: str) -> PipelineOutput:
        timings: list[StageTiming] = []
        usage = _UsageAccumulator()
        tables: list[str] = []
        sql = ""
        explanation = ""

        try:
            with _timed(timings, "retrieve"):
                tables = await self._retriever.retrieve(question)

            with _timed(timings, "render"):
                ddl = render_ddl(self._graph, tables)

            prompt = self._builder.generate(question, ddl)
            with _timed(timings, "generate"):
                completion = await self._llm.complete(prompt.system, prompt.user)
            usage.add(completion)

            with _timed(timings, "parse"):
                sql, explanation = parse_llm_output(completion.text)

            with _timed(timings, "guard"):
                guarded_sql = guard_sql(sql, set(tables), max_rows=self._settings.max_rows)
            sql = guarded_sql

            try:
                with _timed(timings, "execute"):
                    result = await execute_readonly(
                        self._target_engine,
                        guarded_sql,
                        statement_timeout_ms=self._settings.statement_timeout_ms,
                        max_rows=self._settings.max_rows,
                    )
            except ExecutionError as exc:
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
                    )
                return await self._repair(question, ddl, guarded_sql, exc, tables, usage, timings)

            return self._finish(
                sql=sql,
                explanation=explanation,
                result=result,
                tables=tables,
                repaired=False,
                usage=usage,
                timings=timings,
                error=None,
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
            )
        except GuardError as exc:
            return self._finish(
                sql=sql,
                explanation=explanation,
                result=None,
                tables=tables,
                repaired=False,
                usage=usage,
                timings=timings,
                error=f"guard:{exc.reason}: {exc.detail}",
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
            )
        except GuardError as inner:
            self._log_repair_stage(timings, start)
            return self._finish(
                sql=sql,
                explanation=explanation,
                result=None,
                tables=tables,
                repaired=True,
                usage=usage,
                timings=timings,
                error=f"guard:{inner.reason}: {inner.detail}",
            )

        self._log_repair_stage(timings, start)

        try:
            with _timed(timings, "execute"):
                result = await execute_readonly(
                    self._target_engine,
                    guarded_sql,
                    statement_timeout_ms=self._settings.statement_timeout_ms,
                    max_rows=self._settings.max_rows,
                )
        except ExecutionError as inner:
            return self._finish(
                sql=guarded_sql,
                explanation=explanation,
                result=None,
                tables=tables,
                repaired=True,
                usage=usage,
                timings=timings,
                error=f"execution:{inner.kind}: {inner.pg_message}",
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
        )

    @staticmethod
    def _log_repair_stage(timings: list[StageTiming], start: float) -> None:
        ms = (time.perf_counter() - start) * 1000
        timings.append(StageTiming(stage="repair", ms=ms))
        logger.info("stage", extra={"stage": "repair", "ms": ms})

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
        )
