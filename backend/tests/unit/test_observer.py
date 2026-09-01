"""Unit tests for `app.core.observer`: `NullObserver` is a no-op
implementation of `PipelineObserver` -- every call just shouldn't raise."""

from __future__ import annotations

from app.core.observer import NullObserver
from app.llm.base import Usage


def test_null_observer_every_hook_is_a_silent_no_op() -> None:
    observer = NullObserver()

    assert observer.on_stage("retrieve", 1.0) is None
    assert observer.on_llm(stage="generate", model="fake", usage=Usage(0, 0, 0.0)) is None
    assert observer.on_guard_reject("not_select") is None
    assert observer.on_execution(outcome="success", ms=5.0) is None
    assert observer.on_cache(cache="sql", outcome="miss") is None
    assert observer.on_pipeline_done(ms=10.0, ok=True) is None
