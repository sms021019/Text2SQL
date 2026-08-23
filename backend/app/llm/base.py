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


class LLMClient(Protocol):
    async def complete(self, system: str, user: str, *, temperature: float = 0.0) -> Completion: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
