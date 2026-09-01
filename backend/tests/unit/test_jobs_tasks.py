"""`run_query_job` must correlate its log lines the way the API's
`RequestIDMiddleware` does: bind the enqueueing request's `request_id` (and
arq's own `job_id`) into structlog's contextvars *before* doing any work, so
every per-stage line the pipeline emits on the worker carries them.

No container needed -- the binding happens at the very top of the task, so
letting it fail immediately afterwards on a `ctx` with no `AppComponents` is
enough to observe it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from app.jobs.tasks import run_query_job
from app.observability.logging import configure_logging


@pytest.fixture(autouse=True)
def _clean_contextvars() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


async def test_run_query_job_binds_request_id_and_job_id_into_log_context(capsys: Any) -> None:
    configure_logging("INFO")
    # A leftover binding from whatever ran in this worker before: the task
    # must clear it, not add to it.
    structlog.contextvars.bind_contextvars(request_id="stale", extra="stale")

    ctx: dict[Any, Any] = {"job_id": "job-abc"}
    with pytest.raises(RuntimeError, match="no AppComponents"):
        await run_query_job(ctx, "How many orders are there?", request_id="req-123")

    assert structlog.contextvars.get_contextvars() == {
        "request_id": "req-123",
        "job_id": "job-abc",
    }

    # ... and a plain stdlib log line from `app/core/**` (which cannot import
    # structlog) picks both up, which is the point of binding them.
    logging.getLogger("app.core.pipeline").info("stage", extra={"stage": "guard", "ms": 1.0})
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["request_id"] == "req-123"
    assert payload["job_id"] == "job-abc"
    assert payload["stage"] == "guard"
