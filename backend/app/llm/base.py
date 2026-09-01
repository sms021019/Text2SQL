from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage
    model: str


@dataclass(frozen=True)
class EmbeddingResult:
    """One `embed()` call's vectors plus what it cost.

    Mirrors `Completion` so an embedding call can be reported to
    `app.core.observer.PipelineObserver.on_llm(stage="embed", ...)` exactly
    like a completion. `usage.prompt_tokens` is the number of tokens
    embedded where the provider reports it (Ollama's `prompt_eval_count`,
    OpenAI's `usage.prompt_tokens`) and `0` where it does not;
    `completion_tokens` is always `0` -- an embedding generates no output
    tokens. This usage is metrics-only: it is deliberately never folded into
    `PipelineOutput.usage` or `query_log.prompt_tokens` (see ADR 0005).
    """

    vectors: list[list[float]]
    usage: Usage
    model: str


class LLMClient(Protocol):
    async def complete(self, system: str, user: str, *, temperature: float = 0.0) -> Completion: ...

    async def embed(self, texts: list[str]) -> EmbeddingResult: ...
