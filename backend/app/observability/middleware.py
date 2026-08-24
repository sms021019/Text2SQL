"""Request-ID middleware: reads/generates `X-Request-ID`, binds it into
structlog's contextvars for the duration of the request (so every log line
emitted while handling it, including from `app/core/**`'s stdlib logging,
carries `request_id`), echoes it back in the response header, and logs one
`http_request` summary event per request.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

__all__ = ["RequestIDMiddleware"]

logger = structlog.get_logger(__name__)

_HEADER = "X-Request-ID"
_MAX_LEN = 64
_VALID = re.compile(r"^[A-Za-z0-9._-]+$")


def _resolve_request_id(inbound: str | None) -> str:
    """Accept an inbound `X-Request-ID` only if it's a reasonably-shaped
    token (bounded length, safe charset) -- otherwise generate a fresh one
    rather than let an arbitrary client-controlled string flow unbounded
    into logs and the response header."""
    if inbound:
        candidate = inbound[:_MAX_LEN]
        if _VALID.match(candidate):
            return candidate
    return str(uuid.uuid4())


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = _resolve_request_id(request.headers.get(_HEADER))
        request.state.request_id = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        response.headers[_HEADER] = request_id
        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response
