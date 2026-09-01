"""Integration test for the real `create_app()` lifespan wiring of the
startup embedding timeout (`Settings.startup_embed_timeout_s` /
`asyncio.wait_for(retriever.build_index(), ...)` in `app.main`'s lifespan,
added in Task 12 fix round 1).

`tests/unit/test_retrieve.py` exercises `SchemaRetriever.mark_build_failed()`
directly; this test drives the actual `asyncio.wait_for(...)` wrapping
end-to-end, against a real seeded database and an LLM double whose `embed()`
call genuinely hangs -- proving the app comes up promptly, serves a graceful
`llm:` query error while the LLM stays down, and self-heals once it recovers.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.core.errors import LLMError
from app.llm.base import Completion, EmbeddingResult
from app.main import create_app
from tests.fakes.llm import FakeLLM

pytestmark = pytest.mark.integration

GOOD_RESPONSE = json.dumps(
    {"sql": "SELECT count(*) AS n FROM orders", "explanation": "Counts all orders."}
)


class HangingLLM:
    """LLMClient double whose `embed()` genuinely hangs for 10s and then
    fails while `working` is False -- simulating an LLM that is technically
    reachable but too slow to answer, the scenario `startup_embed_timeout_s`
    exists for -- and behaves like a normal, fast `FakeLLM` once flipped.
    `complete()` is always fast, delegated to an internal `FakeLLM`.
    """

    def __init__(self) -> None:
        self.working = False
        self.embed_calls = 0
        self._fake = FakeLLM(responses=[GOOD_RESPONSE] * 5)

    async def complete(self, system: str, user: str, *, temperature: float = 0.0) -> Completion:
        return await self._fake.complete(system, user, temperature=temperature)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.embed_calls += 1
        if not self.working:
            await asyncio.sleep(10)
            raise LLMError("ollama embed request failed: still hanging")
        return await self._fake.embed(texts)


@pytest.fixture
def settings(app_db_url: str, readonly_async_url: str) -> Settings:
    return Settings(
        app_db_url=app_db_url,
        target_db_url=readonly_async_url,
        startup_embed_timeout_s=0.2,
        _env_file=None,
    )


async def test_slow_llm_at_startup_does_not_block_readiness_and_self_heals(
    settings: Settings,
) -> None:
    llm = HangingLLM()
    app = create_app(settings=settings, llm=llm)

    start = time.perf_counter()
    async with LifespanManager(app):
        elapsed = time.perf_counter() - start
        # Well under the LLM's 10s hang and the default llm_timeout_s (60s)
        # -- proves asyncio.wait_for(..., timeout=startup_embed_timeout_s)
        # actually bounds the startup wait rather than falling through to
        # embed()'s own delay.
        assert elapsed < 5.0, f"startup took {elapsed:.2f}s, expected close to 0.2s"

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/readyz")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

            # LLM is still "down" (hanging): the query must fail gracefully
            # at the retrieve stage -- 200 with an `llm:` error, never a 500
            # or an uncaught RuntimeError.
            resp = await client.post(
                "/api/v1/query", json={"question": "how many orders are there"}
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["error"] is not None
            assert body["error"].startswith("llm:"), body["error"]

            # LLM recovers: the next query's lazy retry (SchemaRetriever's
            # self-heal from fix round 1) picks it up and gets past
            # retrieval.
            llm.working = True
            resp = await client.post(
                "/api/v1/query", json={"question": "how many orders are there"}
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["error"] is None or not body["error"].startswith("llm:"), body["error"]
