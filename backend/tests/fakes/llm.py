import hashlib
import math

from app.llm.base import Completion, Usage


class FakeLLM:
    """In-memory LLMClient double for tests.

    `complete()` returns canned responses: pop them in order from a list
    (raising once exhausted), or match by substring against the user prompt
    from a dict. `embed()` is deterministic per input text, derived from its
    sha256 digest, so cosine/L2 similarity assertions are stable across runs.
    """

    def __init__(
        self,
        responses: list[str] | dict[str, str] | None = None,
        dim: int = 8,
    ) -> None:
        self.responses: list[str] | dict[str, str] = responses if responses is not None else []
        self.dim = dim
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, *, temperature: float = 0.0) -> Completion:
        self.calls.append((system, user))
        text = self._next_response(user)
        usage = Usage(
            prompt_tokens=len(user) // 4,
            completion_tokens=len(text) // 4,
            latency_ms=0.0,
        )
        return Completion(text=text, usage=usage, model="fake")

    def _next_response(self, user: str) -> str:
        if isinstance(self.responses, dict):
            for key, value in self.responses.items():
                if key in user:
                    return value
            raise AssertionError("FakeLLM exhausted")
        if not self.responses:
            raise AssertionError("FakeLLM exhausted")
        return self.responses.pop(0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [digest[i % len(digest)] / 255.0 for i in range(self.dim)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]
