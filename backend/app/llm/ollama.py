import time

import httpx

from app.core.errors import LLMError
from app.llm.base import Completion, Usage


class OllamaClient:
    """LLMClient adapter for a local/self-hosted Ollama server."""

    def __init__(
        self,
        base_url: str,
        model: str,
        embed_model: str,
        api_key: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.embed_model = embed_model
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def complete(self, system: str, user: str, *, temperature: float = 0.0) -> Completion:
        start = time.perf_counter()
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    "options": {"temperature": temperature},
                },
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama chat request failed: {exc}") from exc
        latency_ms = (time.perf_counter() - start) * 1000
        if resp.is_error:
            raise LLMError(f"ollama chat failed: {resp.status_code} {resp.text}")
        try:
            data = resp.json()
            text: str = data["message"]["content"]
            usage = Usage(
                prompt_tokens=data.get("prompt_eval_count", 0),
                completion_tokens=data.get("eval_count", 0),
                latency_ms=latency_ms,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"ollama chat returned an unexpected response shape: {exc}") from exc
        return Completion(text=text, usage=usage, model=self.model)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.embed_model, "input": texts},
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama embed request failed: {exc}") from exc
        if resp.is_error:
            raise LLMError(f"ollama embed failed: {resp.status_code} {resp.text}")
        try:
            data = resp.json()
            embeddings: list[list[float]] = data["embeddings"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"ollama embed returned an unexpected response shape: {exc}") from exc
        return embeddings

    async def aclose(self) -> None:
        await self._client.aclose()
