"""Observer hooks the pipeline calls out to at each stage, so `app/core/**`
can report on its own progress without depending on *how* that gets turned
into metrics, logs, or anything else.

`PipelineObserver` is a structural (duck-typed) `Protocol` -- any object
exposing these methods satisfies it, no inheritance required (see
`app.observability.metrics.MetricsObserver`, added in a later Phase 2 task,
for the real implementation). `NullObserver` is the default every
`Text2SQLPipeline` falls back to when no observer is injected: every method
is a no-op, so observability is opt-in and free when unused.

Kept free of every import except `typing` and `app.llm.base.Usage` --
`app/core/pipeline.py` already depends on `app.llm.base` for `Usage`, so
this does not widen `app/core`'s dependency surface (see
`app/core/errors.py` for the "no fastapi/redis/app.api" rule this module
must not break).
"""

from __future__ import annotations

from typing import Protocol

from app.llm.base import Usage

__all__ = ["NullObserver", "PipelineObserver"]


class PipelineObserver(Protocol):
    def on_stage(self, stage: str, ms: float) -> None: ...

    def on_llm(self, *, stage: str, model: str, usage: Usage) -> None: ...

    def on_guard_reject(self, reason: str) -> None: ...

    #: `outcome`: `"success"`, `"db_error"`, `"timeout"`, or `"rejected"`
    #: (a guard rejection short-circuiting execution -- `ms` is `0` then).
    def on_execution(self, *, outcome: str, ms: float) -> None: ...

    #: `cache`: `"sql"`, `"result"`, or `"schema"`. `outcome`: `"hit"`,
    #: `"miss"`, or `"bypass"` (the tier was not attempted at all, e.g.
    #: `use_result_cache=False`).
    def on_cache(self, *, cache: str, outcome: str) -> None: ...

    def on_pipeline_done(self, *, ms: float, ok: bool) -> None: ...


class NullObserver:
    """A `PipelineObserver` whose every method does nothing. The default
    for `Text2SQLPipeline` when no observer is injected."""

    def on_stage(self, stage: str, ms: float) -> None:
        pass

    def on_llm(self, *, stage: str, model: str, usage: Usage) -> None:
        pass

    def on_guard_reject(self, reason: str) -> None:
        pass

    def on_execution(self, *, outcome: str, ms: float) -> None:
        pass

    def on_cache(self, *, cache: str, outcome: str) -> None:
        pass

    def on_pipeline_done(self, *, ms: float, ok: bool) -> None:
        pass
