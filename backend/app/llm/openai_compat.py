import time

import httpx

from app.core.errors import LLMError
from app.llm.base import Completion, Usage


class OpenAICompatClient:
    """LLMClient adapter for OpenAI and OpenAI-compatible chat/embeddings APIs."""

    def __init__(
        self,
        base_url: str,
        model: str,
        embed_model: str,
        api_key: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base = base_url.rstrip("/")
        # Normalise to a base URL with no trailing "/v1" so we can always
        # append "/v1/..." below without doubling it.
        self.base_url = base[: -len("/v1")] if base.endswith("/v1") else base
        self.model = model
        self.embed_model = embed_model
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def complete(self, system: str, user: str, *, temperature: float = 0.0) -> Completion:
        start = time.perf_counter()
        try:
            resp = await self._client.post(
                f"{self.base_url}/v1/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                },
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"openai chat request failed: {exc}") from exc
        latency_ms = (time.perf_counter() - start) * 1000
        if resp.is_error:
            raise LLMError(f"openai chat failed: {resp.status_code} {resp.text}")
        data = resp.json()
        text: str = data["choices"][0]["message"]["content"]
        usage_data = data.get("usage", {})
        usage = Usage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            latency_ms=latency_ms,
        )
        return Completion(text=text, usage=usage, model=data.get("model", self.model))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = await self._client.post(
                f"{self.base_url}/v1/embeddings",
                headers=self._headers(),
                json={"model": self.embed_model, "input": texts},
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"openai embed request failed: {exc}") from exc
        if resp.is_error:
            raise LLMError(f"openai embed failed: {resp.status_code} {resp.text}")
        data = resp.json()
        items = sorted(data["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in items]
