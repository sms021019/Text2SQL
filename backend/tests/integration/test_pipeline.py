"""Integration tests for the end-to-end Text2SQL pipeline.

Uses a real seeded database (via `readonly_async_url`) for schema
introspection and query execution, but a `FakeLLM` for every completion, so
each test controls exactly what the "model" says and can assert on
`FakeLLM.calls`.
"""

import json

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.core.pipeline import PipelineOutput, Text2SQLPipeline
from app.core.prompting.builder import PromptBuilder
from app.core.schema.introspect import introspect
from app.core.schema.retrieve import SchemaRetriever
from tests.fakes.llm import FakeLLM

pytestmark = pytest.mark.integration

GOOD_RESPONSE = json.dumps(
    {"sql": "SELECT count(*) AS n FROM orders", "explanation": "Counts all orders."}
)
#: Valid syntax, references a real table, but a column that does not exist --
#: passes the guard (which only validates table names) and fails at the
#: database with a SQLSTATE the executor classifies as "syntax", so it is a
#: repairable `ExecutionError` rather than a terminal `GuardError`.
BAD_COLUMN_RESPONSE = json.dumps(
    {
        "sql": "SELECT count(*) AS n FROM orders WHERE ordr_status = 'paid'",
        "explanation": "Counts paid orders.",
    }
)


@pytest.fixture(scope="module")
async def graph_and_retriever(readonly_async_url: str) -> tuple:
    engine = create_async_engine(readonly_async_url)
    try:
        graph = await introspect(engine)
    finally:
        await engine.dispose()
    retriever = SchemaRetriever(graph, FakeLLM())
    await retriever.build_index()
    return graph, retriever


def _make_pipeline(
    graph_and_retriever: tuple, readonly_async_url: str, responses: list[str]
) -> tuple[Text2SQLPipeline, FakeLLM, object]:
    graph, retriever = graph_and_retriever
    llm = FakeLLM(responses)
    engine = create_async_engine(readonly_async_url)
    pipeline = Text2SQLPipeline(
        llm=llm,
        retriever=retriever,
        graph=graph,
        builder=PromptBuilder(),
        target_engine=engine,
        settings=Settings(),
    )
    return pipeline, llm, engine


async def test_happy_path_returns_result(
    graph_and_retriever: tuple, readonly_async_url: str
) -> None:
    pipeline, llm, engine = _make_pipeline(graph_and_retriever, readonly_async_url, [GOOD_RESPONSE])
    try:
        output = await pipeline.run("How many orders are there?")
    finally:
        await engine.dispose()

    assert isinstance(output, PipelineOutput)
    assert output.error is None
    assert output.repaired is False
    assert output.result is not None
    assert output.result.rows == [[20000]]
    assert "LIMIT" in output.sql.upper()
    assert output.explanation == "Counts all orders."
    assert "orders" in output.tables
    assert output.model == "fake"
    assert len(llm.calls) == 1
    assert [t.stage for t in output.timings] == [
        "retrieve",
        "render",
        "generate",
        "parse",
        "guard",
        "execute",
    ]
    assert all(t.ms >= 0 for t in output.timings)
    assert output.usage.prompt_tokens > 0
    assert output.usage.completion_tokens > 0


async def test_repair_path_succeeds_after_one_bad_attempt(
    graph_and_retriever: tuple, readonly_async_url: str
) -> None:
    pipeline, llm, engine = _make_pipeline(
        graph_and_retriever, readonly_async_url, [BAD_COLUMN_RESPONSE, GOOD_RESPONSE]
    )
    try:
        output = await pipeline.run("How many orders are there?")
    finally:
        await engine.dispose()

    assert output.error is None
    assert output.repaired is True
    assert output.result is not None
    assert output.result.rows == [[20000]]
    assert len(llm.calls) == 2
    assert [t.stage for t in output.timings] == [
        "retrieve",
        "render",
        "generate",
        "parse",
        "guard",
        "execute",
        "repair",
        "execute",
    ]


async def test_guard_rejection_is_terminal_with_no_repair_attempt(
    graph_and_retriever: tuple, readonly_async_url: str
) -> None:
    pipeline, llm, engine = _make_pipeline(
        graph_and_retriever, readonly_async_url, ["DELETE FROM orders"]
    )
    try:
        output = await pipeline.run("Delete all orders")
    finally:
        await engine.dispose()

    assert output.error is not None
    assert output.error.startswith("guard:not_select")
    assert output.result is None
    assert output.repaired is False
    assert len(llm.calls) == 1


async def test_repair_exhausted_returns_execution_error(
    graph_and_retriever: tuple, readonly_async_url: str
) -> None:
    pipeline, llm, engine = _make_pipeline(
        graph_and_retriever, readonly_async_url, [BAD_COLUMN_RESPONSE, BAD_COLUMN_RESPONSE]
    )
    try:
        output = await pipeline.run("How many orders are there?")
    finally:
        await engine.dispose()

    assert output.error is not None
    assert output.error.startswith("execution:")
    assert output.result is None
    assert output.repaired is True
    assert len(llm.calls) == 2
