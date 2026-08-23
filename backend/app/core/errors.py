class DomainError(Exception):
    """Base class for all domain-level errors.

    `app/core/**` must never import fastapi/redis/app.api — this module (and
    everything under `app/core`) stays a plain-Python layer so it can be
    imported from anywhere without pulling in web-framework or infra deps.
    """


class LLMError(DomainError):
    """Raised when an LLM provider call fails (non-2xx response, transport
    error, etc.)."""
