import pytest

from app.core.schema.models import Column, ForeignKey, SchemaGraph, Table
from app.core.schema.retrieve import SchemaRetriever
from tests.fakes.llm import FakeLLM


class VectorLLM(FakeLLM):
    """FakeLLM whose embed() returns hand-picked vectors for known texts.

    Falls back to FakeLLM's sha256-based embedding for anything not listed,
    so it stays a drop-in LLMClient double. Also records every embed() call
    so tests can assert on batching behaviour.
    """

    def __init__(self, vectors: dict[str, list[float]], dim: int = 4) -> None:
        super().__init__(dim=dim)
        self._vectors = vectors
        self.embed_calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return await super().embed(texts)

    def _embed_one(self, text: str) -> list[float]:
        if text in self._vectors:
            return self._vectors[text]
        return super()._embed_one(text)


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
