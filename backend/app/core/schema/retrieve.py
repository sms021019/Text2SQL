"""Embedding-based schema retrieval with FK-hop expansion and keyword boost.

`app/core/**` must never import fastapi/redis/app.api (see
`app/core/errors.py`), so this module only depends on numpy and the plain
`LLMClient` protocol.
"""

from __future__ import annotations

import re

import numpy as np
from numpy.typing import NDArray

from app.core.schema.models import SchemaGraph
from app.llm.base import LLMClient

_KEYWORD_BOOST = 0.2
_NOT_INDEXED = "build_index() has not been called"


class SchemaRetriever:
    """Selects the tables most relevant to a natural-language question.

    Tables are ranked by cosine similarity between the question embedding
    and each table's `summary()` embedding, plus a keyword boost for any
    table or column mentioned by name in the question. The top-k selection
    is then expanded with `hops` levels of foreign-key neighbours.
    """

    def __init__(
        self,
        graph: SchemaGraph,
        llm: LLMClient,
        *,
        top_k: int = 4,
        hops: int = 1,
    ) -> None:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        self._graph = graph
        self._llm = llm
        self._top_k = top_k
        self._hops = hops
        self._table_names: list[str] | None = None
        self._embeddings: NDArray[np.float64] | None = None

    async def build_index(self) -> None:
        names = sorted(self._graph.tables)
        summaries = [self._graph.tables[name].summary() for name in names]
        vectors = await self._llm.embed(summaries)
        self._table_names = names
        self._embeddings = np.array(vectors, dtype=np.float64)

    async def scores(self, question: str) -> dict[str, float]:
        names, combined = await self._score_tables(question)
        return dict(zip(names, (float(v) for v in combined), strict=True))

    async def retrieve(self, question: str) -> list[str]:
        names, combined = await self._score_tables(question)
        order = sorted(range(len(names)), key=lambda i: (-combined[i], names[i]))
        top = [names[i] for i in order[: self._top_k]]

        visited = set(top)
        frontier = set(top)
        for _ in range(self._hops):
            next_frontier: set[str] = set()
            for name in frontier:
                next_frontier |= self._graph.neighbors(name)
            next_frontier -= visited
            visited |= next_frontier
            frontier = next_frontier

        neighbours = sorted(visited - set(top))
        return top + neighbours

    async def _score_tables(self, question: str) -> tuple[list[str], NDArray[np.float64]]:
        if self._table_names is None or self._embeddings is None:
            raise RuntimeError(_NOT_INDEXED)
        [question_vec] = await self._llm.embed([question])
        q = np.array(question_vec, dtype=np.float64)

        table_norms = np.linalg.norm(self._embeddings, axis=1)
        q_norm = np.linalg.norm(q)
        denom = table_norms * q_norm
        with np.errstate(invalid="ignore", divide="ignore"):
            cosine = np.where(denom > 0, (self._embeddings @ q) / denom, 0.0)

        boosts = np.array(
            [self._keyword_boost(name, question) for name in self._table_names],
            dtype=np.float64,
        )
        return self._table_names, cosine + boosts

    def _keyword_boost(self, table_name: str, question: str) -> float:
        lowered = question.lower()
        table = self._graph.tables[table_name]
        candidates = {table_name, *(c.name for c in table.columns)}
        if table_name.endswith("s"):
            candidates.add(table_name[:-1])
        for word in candidates:
            if re.search(rf"\b{re.escape(word.lower())}\b", lowered):
                return _KEYWORD_BOOST
        return 0.0
