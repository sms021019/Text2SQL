import numpy as np
import pytest

from app.core.errors import LLMError
from app.core.observer import NullObserver
from app.core.schema.models import Column, ForeignKey, SchemaGraph, Table
from app.core.schema.retrieve import SchemaRetriever
from app.llm.base import EmbeddingResult, Usage
from tests.fakes.llm import FakeLLM


class VectorLLM(FakeLLM):
    """FakeLLM whose embed() returns hand-picked vectors for known texts.

    Falls back to FakeLLM's sha256-based embedding for anything not listed,
    so it stays a drop-in LLMClient double. `FakeLLM.embed()` already records
    every call in `embed_calls`, so tests can assert on batching behaviour.
    """

    def __init__(self, vectors: dict[str, list[float]], dim: int = 4) -> None:
        super().__init__(dim=dim)
        self._vectors = vectors

    def _embed_one(self, text: str) -> list[float]:
        if text in self._vectors:
            return self._vectors[text]
        return super()._embed_one(text)


class FlakyLLM(VectorLLM):
    """VectorLLM whose embed() raises `LLMError` for the first `fail_times`
    calls, then embeds normally -- simulates the LLM being unreachable (e.g.
    Ollama not up yet in `docker compose`) and later recovering (e.g. once
    `ollama-pull` finishes).
    """

    def __init__(self, vectors: dict[str, list[float]], fail_times: int, dim: int = 4) -> None:
        super().__init__(vectors, dim=dim)
        self._remaining_failures = fail_times

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            self.embed_calls.append(list(texts))
            raise LLMError("ollama embed request failed: connection refused")
        return await super().embed(texts)


class RecordingObserver(NullObserver):
    """`PipelineObserver` that records every `on_llm` event, so a test can
    assert the retriever reported its embedding calls."""

    def __init__(self) -> None:
        self.llm_calls: list[tuple[str, str, Usage]] = []

    def on_llm(self, *, stage: str, model: str, usage: Usage) -> None:
        self.llm_calls.append((stage, model, usage))


def _customers() -> Table:
    return Table(
        name="customers",
        comment="End customers.",
        columns=[
            Column(name="id", type="BIGINT", nullable=False, comment=None, is_pk=True),
            Column(name="email", type="TEXT", nullable=False, comment=None, is_pk=False),
        ],
        foreign_keys=[],
    )


def _orders() -> Table:
    return Table(
        name="orders",
        comment="Customer orders.",
        columns=[
            Column(name="id", type="BIGINT", nullable=False, comment=None, is_pk=True),
            Column(name="customer_id", type="BIGINT", nullable=False, comment=None, is_pk=False),
        ],
        foreign_keys=[ForeignKey(column="customer_id", ref_table="customers", ref_column="id")],
    )


def _products() -> Table:
    return Table(
        name="products",
        comment="Catalog products.",
        columns=[
            Column(name="id", type="BIGINT", nullable=False, comment=None, is_pk=True),
            Column(name="name", type="TEXT", nullable=False, comment=None, is_pk=False),
        ],
        foreign_keys=[],
    )


def _graph() -> SchemaGraph:
    return SchemaGraph.build(
        tables={"orders": _orders(), "customers": _customers(), "products": _products()}
    )


ORDERS_VEC = [1.0, 0.0, 0.0, 0.0]
CUSTOMERS_VEC = [0.0, 1.0, 0.0, 0.0]
PRODUCTS_VEC = [0.0, 0.0, 1.0, 0.0]


def _base_vectors() -> dict[str, list[float]]:
    return {
        _orders().summary(): ORDERS_VEC,
        _customers().summary(): CUSTOMERS_VEC,
        _products().summary(): PRODUCTS_VEC,
    }


async def test_retrieve_orders_question_returns_orders_then_customers_via_fk() -> None:
    graph = _graph()
    vectors = _base_vectors()
    vectors["how many orders"] = ORDERS_VEC
    llm = VectorLLM(vectors)
    retriever = SchemaRetriever(graph, llm, top_k=1, hops=1)
    await retriever.build_index()

    result = await retriever.retrieve("how many orders")

    assert result[0] == "orders"
    assert "customers" in result
    assert "products" not in result
    assert len(result) <= 1 + len(graph.neighbors("orders"))


async def test_retrieve_before_build_index_raises_runtime_error() -> None:
    graph = _graph()
    llm = VectorLLM({})
    retriever = SchemaRetriever(graph, llm)

    with pytest.raises(RuntimeError, match=r"build_index\(\) has not been called"):
        await retriever.retrieve("how many orders")


async def test_scores_before_build_index_raises_runtime_error() -> None:
    graph = _graph()
    llm = VectorLLM({})
    retriever = SchemaRetriever(graph, llm)

    with pytest.raises(RuntimeError, match=r"build_index\(\) has not been called"):
        await retriever.scores("how many orders")


def test_top_k_must_be_positive() -> None:
    graph = _graph()
    llm = VectorLLM({})

    with pytest.raises(ValueError):
        SchemaRetriever(graph, llm, top_k=0)


async def test_keyword_boost_matches_singular_form() -> None:
    graph = _graph()
    zero_sim_vec = [0.0, 0.0, 0.0, 1.0]
    vectors = _base_vectors()
    vectors["show me the order status"] = zero_sim_vec
    llm = VectorLLM(vectors)
    retriever = SchemaRetriever(graph, llm, top_k=1, hops=0)
    await retriever.build_index()

    scores = await retriever.scores("show me the order status")
    assert scores["orders"] == pytest.approx(0.2)
    assert scores["customers"] == pytest.approx(0.0)
    assert scores["products"] == pytest.approx(0.0)

    result = await retriever.retrieve("show me the order status")
    assert result == ["orders"]


async def test_keyword_boost_matches_column_name() -> None:
    graph = _graph()
    zero_sim_vec = [0.0, 0.0, 0.0, 1.0]
    vectors = _base_vectors()
    vectors["what is the customer_id for this row"] = zero_sim_vec
    llm = VectorLLM(vectors)
    retriever = SchemaRetriever(graph, llm, top_k=1, hops=0)
    await retriever.build_index()

    scores = await retriever.scores("what is the customer_id for this row")
    assert scores["orders"] == pytest.approx(0.2)


async def test_keyword_boost_requires_whole_word_match() -> None:
    graph = _graph()
    zero_sim_vec = [0.0, 0.0, 0.0, 1.0]
    vectors = _base_vectors()
    # "reorders" contains "order" as a substring but not as a whole word.
    vectors["please reorders the list"] = zero_sim_vec
    llm = VectorLLM(vectors)
    retriever = SchemaRetriever(graph, llm, top_k=1, hops=0)
    await retriever.build_index()

    scores = await retriever.scores("please reorders the list")
    assert scores["orders"] == pytest.approx(0.0)


async def test_retrieve_dedupes_top_and_neighbour_overlap() -> None:
    graph = _graph()
    vectors = _base_vectors()
    vectors["orders and customers"] = [0.9, 0.9, 0.0, 0.0]
    llm = VectorLLM(vectors)
    retriever = SchemaRetriever(graph, llm, top_k=2, hops=1)
    await retriever.build_index()

    result = await retriever.retrieve("orders and customers")

    assert set(result) == {"orders", "customers"}
    assert result.count("customers") == 1
    assert result.count("orders") == 1


async def test_build_index_embeds_all_summaries_in_a_single_call() -> None:
    graph = _graph()
    llm = VectorLLM(_base_vectors())
    retriever = SchemaRetriever(graph, llm)

    await retriever.build_index()

    assert len(llm.embed_calls) == 1
    assert set(llm.embed_calls[0]) == {t.summary() for t in graph.tables.values()}


async def test_retriever_reports_embed_calls_to_the_observer() -> None:
    graph = _graph()
    vectors = _base_vectors()
    vectors["how many customers?"] = CUSTOMERS_VEC
    observer = RecordingObserver()
    retriever = SchemaRetriever(graph, VectorLLM(vectors), top_k=4, observer=observer)

    await retriever.build_index()
    await retriever.retrieve("how many customers?")

    stages = [stage for stage, _, _ in observer.llm_calls]
    assert stages == ["embed", "embed"]  # one for the index, one for the question
    assert all(model == "fake-embed" for _, model, _ in observer.llm_calls)
    assert observer.llm_calls[0][2].prompt_tokens == len(graph.tables)
    assert observer.llm_calls[1][2].prompt_tokens == 1


async def test_build_index_reports_to_the_observer_argument_when_given() -> None:
    """`prepare_retriever()` hands `build_index()` the startup/refresh-only
    schema observer, keeping that one embed off the per-request pipeline
    observer -- see `app.services.bootstrap._resolve_observers`."""
    graph = _graph()
    vectors = _base_vectors()
    vectors["how many customers?"] = CUSTOMERS_VEC
    request_observer = RecordingObserver()
    build_observer = RecordingObserver()
    retriever = SchemaRetriever(graph, VectorLLM(vectors), top_k=4, observer=request_observer)

    await retriever.build_index(observer=build_observer)
    await retriever.retrieve("how many customers?")

    assert [stage for stage, _, _ in build_observer.llm_calls] == ["embed"]
    assert [stage for stage, _, _ in request_observer.llm_calls] == ["embed"]
    assert build_observer.llm_calls[0][2].prompt_tokens == len(graph.tables)
    assert request_observer.llm_calls[0][2].prompt_tokens == 1


async def test_failed_build_index_reports_no_embed_event() -> None:
    graph = _graph()
    observer = RecordingObserver()
    retriever = SchemaRetriever(graph, FlakyLLM(_base_vectors(), fail_times=99), observer=observer)

    await retriever.build_index()

    assert observer.llm_calls == []


async def test_scores_covers_all_tables() -> None:
    graph = _graph()
    llm = VectorLLM(_base_vectors())
    retriever = SchemaRetriever(graph, llm)
    await retriever.build_index()

    result = await retriever.scores("anything")

    assert set(result.keys()) == {"orders", "customers", "products"}


async def test_retrieve_zero_hops_does_not_expand_neighbours() -> None:
    graph = _graph()
    vectors = _base_vectors()
    vectors["how many orders"] = ORDERS_VEC
    llm = VectorLLM(vectors)
    retriever = SchemaRetriever(graph, llm, top_k=1, hops=0)
    await retriever.build_index()

    result = await retriever.retrieve("how many orders")

    assert result == ["orders"]


async def test_build_index_tolerates_llm_error_and_does_not_raise() -> None:
    graph = _graph()
    llm = FlakyLLM(_base_vectors(), fail_times=99)
    retriever = SchemaRetriever(graph, llm)

    await retriever.build_index()  # LLM unreachable -- must not raise

    assert len(llm.embed_calls) == 1


async def test_retrieve_raises_llm_error_while_down_then_succeeds_after_recovery() -> None:
    graph = _graph()
    vectors = _base_vectors()
    vectors["how many orders"] = ORDERS_VEC
    # Fails the initial build_index() call and the first lazy retry inside
    # retrieve(), then recovers -- exercising both halves of the self-heal
    # path in one retriever instance.
    llm = FlakyLLM(vectors, fail_times=2)
    retriever = SchemaRetriever(graph, llm, top_k=1, hops=0)

    await retriever.build_index()  # LLM down: tolerated, does not raise

    with pytest.raises(LLMError):
        await retriever.retrieve("how many orders")  # still down: lazy retry also fails

    result = await retriever.retrieve("how many orders")  # LLM recovered: lazy retry succeeds
    assert result == ["orders"]


async def test_scores_also_raises_llm_error_while_down() -> None:
    graph = _graph()
    llm = FlakyLLM(_base_vectors(), fail_times=99)
    retriever = SchemaRetriever(graph, llm)

    await retriever.build_index()

    with pytest.raises(LLMError):
        await retriever.scores("how many orders")


async def test_mark_build_failed_then_retrieve_self_heals_once_llm_recovers() -> None:
    """`mark_build_failed()` (used by `app.main`'s lifespan when a startup
    `asyncio.wait_for(retriever.build_index(), timeout=...)` times out) must
    put the retriever in exactly the state a caught `LLMError` inside
    `build_index()` itself would: `_score_tables()` retries lazily on the
    next call.
    """
    graph = _graph()
    vectors = _base_vectors()
    vectors["how many orders"] = ORDERS_VEC
    llm = VectorLLM(vectors)
    retriever = SchemaRetriever(graph, llm, top_k=1, hops=0)

    retriever.mark_build_failed("embedding timed out during startup after 20.0s")
    assert len(llm.embed_calls) == 0  # build_index() itself was never actually run

    result = await retriever.retrieve("how many orders")  # lazy retry succeeds
    assert result == ["orders"]


async def test_mark_build_failed_surfaces_as_llm_error_while_still_down() -> None:
    graph = _graph()
    llm = FlakyLLM(_base_vectors(), fail_times=99)
    retriever = SchemaRetriever(graph, llm)

    retriever.mark_build_failed("embedding timed out during startup after 20.0s")

    with pytest.raises(LLMError):
        await retriever.retrieve("how many orders")


def test_export_index_returns_none_before_build() -> None:
    graph = _graph()
    llm = VectorLLM({})
    retriever = SchemaRetriever(graph, llm)

    assert retriever.export_index() is None


async def test_export_index_returns_names_and_matrix_after_build() -> None:
    graph = _graph()
    llm = VectorLLM(_base_vectors())
    retriever = SchemaRetriever(graph, llm)
    await retriever.build_index()

    exported = retriever.export_index()

    assert exported is not None
    names, matrix = exported
    assert set(names) == {"orders", "customers", "products"}
    assert matrix.shape == (3, 4)


def test_import_index_raises_on_length_mismatch() -> None:
    graph = _graph()
    llm = VectorLLM({})
    retriever = SchemaRetriever(graph, llm)

    with pytest.raises(ValueError, match="names"):
        retriever.import_index(["orders", "customers"], np.zeros((3, 4)))


def test_import_index_raises_on_unknown_table_name() -> None:
    graph = _graph()
    llm = VectorLLM({})
    retriever = SchemaRetriever(graph, llm)

    with pytest.raises(ValueError, match="unknown"):
        retriever.import_index(["orders", "not_a_table"], np.zeros((2, 4)))


async def test_import_index_marks_built_and_clears_prior_build_error() -> None:
    graph = _graph()
    llm = VectorLLM({})
    retriever = SchemaRetriever(graph, llm, top_k=1, hops=0)
    retriever.mark_build_failed("boom")

    matrix = np.array([ORDERS_VEC, CUSTOMERS_VEC, PRODUCTS_VEC])
    retriever.import_index(["orders", "customers", "products"], matrix)

    exported = retriever.export_index()
    assert exported is not None
    names, exported_matrix = exported
    assert names == ["orders", "customers", "products"]
    assert exported_matrix.shape == (3, 4)

    # build_error was cleared, so retrieve() no longer raises LLMError from
    # the stale mark_build_failed() state -- it uses the imported index.
    result = await retriever.retrieve("how many orders")
    assert result and set(result) <= {"orders", "customers", "products"}
