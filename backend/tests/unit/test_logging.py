"""`configure_logging` must route stdlib `extra={...}` fields (e.g. the
pipeline's `stage`/`ms` fields) into the rendered JSON, not just structlog's
native keyword events -- see `app.observability.logging`'s docstring on why
`app.core.pipeline` etc. use stdlib `logging` instead of structlog directly.
"""

from __future__ import annotations

import json
import logging

import structlog

from app.observability.logging import configure_logging


def test_stdlib_extra_fields_are_rendered_in_json(capsys):
    configure_logging("INFO")
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="probe")
    try:
        logging.getLogger("app.core.pipeline").info("stage", extra={"stage": "guard", "ms": 1.8})
    finally:
        structlog.contextvars.clear_contextvars()

    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    payload = json.loads(out[0])
    assert payload["stage"] == "guard"
    assert payload["ms"] == 1.8
    assert payload["request_id"] == "probe"
    assert payload["event"] == "stage"
