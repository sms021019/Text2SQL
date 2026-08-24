"""App-layer services that sit above `app/core/**` and below the FastAPI
routers -- shared logic used from more than one entry point (today:
`app.main`'s lifespan and `POST /api/v1/schema/refresh`; a later phase adds
the arq worker) so it lives in exactly one place instead of being copied.
"""
