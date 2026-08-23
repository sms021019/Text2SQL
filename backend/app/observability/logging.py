"""structlog configuration: JSON lines to stdout, request-scoped contextvars
merged into every event, ISO-8601 timestamps, and the level taken from
`Settings.log_level`.

Also routes stdlib `logging` (used by `app.core.pipeline`, `app.core.schema`,
etc. -- see their "must never import fastapi" constraint, which rules out a
structlog dependency there) through structlog's `ProcessorFormatter`, so a
plain `logging.getLogger(__name__).info(...)` call from those modules is
rendered as the same JSON shape as a structlog event.
"""

from __future__ import annotations

import logging
import sys

import structlog

__all__ = ["configure_logging"]

#: Processors that run on *every* event, structlog-native or funnelled in
#: from stdlib logging via `foreign_pre_chain` below.
_SHARED_PROCESSORS: list[structlog.types.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso"),
]


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib `logging` for JSON output at `level`.

    Safe to call more than once (e.g. across `create_app()` calls in tests)
    -- each call replaces the root logger's handlers rather than stacking
    new ones.
    """
    level_value = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level_value),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=_SHARED_PROCESSORS,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level_value)
