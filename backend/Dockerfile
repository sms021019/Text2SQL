# Stage 1: resolve and install dependencies with uv, then install the
# project itself. Build context is the repo root (see docker-compose.yml)
# so both `backend/` and `seed/` can be copied into the image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev

# Stage 2: slim runtime image, non-root user. Pinned to bookworm to match
# the builder stage's Debian version -- `python:3.12-slim` floats to newer
# Debian releases (trixie), and running the builder's bookworm-linked C
# extensions (numpy, asyncpg, psycopg-binary, uvloop, ...) there segfaults
# silently during app startup.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app
COPY --chown=app:app seed/questions.yaml /app/seed/questions.yaml

ENV PATH=/app/.venv/bin:$PATH \
    EXAMPLES_PATH=/app/seed/questions.yaml \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=90s \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

# --no-access-log: app/observability/middleware.py already logs an
# equivalent structured `http_request` JSON event per request, so uvicorn's
# own (non-JSON) access log would just be a redundant, differently-shaped
# duplicate of it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
