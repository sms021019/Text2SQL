import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pipeline import PipelineOutput, StageTiming
from app.core.sql.executor import QueryResult
from app.db.migrate import upgrade_to_head
from app.db.models import QueryLog
from app.db.query_log import record_query
from app.db.session import make_engine
from app.llm.base import Usage

pytestmark = pytest.mark.integration


def _fake_output(*, error: str | None = None, repaired: bool = False) -> PipelineOutput:
    result = None
    if error is None:
        result = QueryResult(
            columns=["id"],
            rows=[[1]],
            row_count=1,
            truncated=False,
            duration_ms=1.5,
        )
    return PipelineOutput(
        sql="SELECT id FROM customers LIMIT 1",
        explanation="fetch a customer id",
        result=result,
        tables=["customers"],
        repaired=repaired,
        usage=Usage(prompt_tokens=10, completion_tokens=5, latency_ms=42.0),
        timings=[StageTiming(stage="retrieve", ms=1.0), StageTiming(stage="generate", ms=2.5)],
        error=error,
        model="qwen2.5-coder:7b",
        cache_status="disabled",
    )


async def test_migration_creates_table_and_record_query_round_trips(app_db_url: str) -> None:
    upgrade_to_head(app_db_url)

    engine = make_engine(app_db_url)
    try:
        request_id = str(uuid.uuid4())
        out = _fake_output()
        async with AsyncSession(engine) as session:
            row = await record_query(
                session,
                out,
                question="how many customers are there?",
                model="qwen2.5-coder:7b",
                schema_version="v1",
                request_id=request_id,
            )
            row_id = row.id

        async with AsyncSession(engine) as session:
            fetched = (
                await session.execute(select(QueryLog).where(QueryLog.id == row_id))
            ).scalar_one()

        assert fetched.id == row_id
        assert fetched.question == "how many customers are there?"
        assert fetched.sql == "SELECT id FROM customers LIMIT 1"
        assert fetched.tables == ["customers"]
        assert fetched.success is True
        assert fetched.error is None
        assert fetched.repaired is False
        assert fetched.latency_ms == pytest.approx(3.5)
        assert fetched.prompt_tokens == 10
        assert fetched.completion_tokens == 5
        assert fetched.model == "qwen2.5-coder:7b"
        assert fetched.schema_version == "v1"
        assert fetched.request_id == request_id
        assert fetched.created_at is not None
    finally:
        await engine.dispose()


async def test_record_query_captures_failed_pipeline(app_db_url: str) -> None:
    upgrade_to_head(app_db_url)

    engine = make_engine(app_db_url)
    try:
        out = _fake_output(error="execution:syntax: relation does not exist", repaired=True)
        async with AsyncSession(engine) as session:
            row = await record_query(
                session,
                out,
                question="bogus question",
                model="qwen2.5-coder:7b",
                schema_version="v1",
                request_id=str(uuid.uuid4()),
            )
            row_id = row.id

        async with AsyncSession(engine) as session:
            fetched = (
                await session.execute(select(QueryLog).where(QueryLog.id == row_id))
            ).scalar_one()

        assert fetched.success is False
        assert fetched.error == "execution:syntax: relation does not exist"
        assert fetched.repaired is True
    finally:
        await engine.dispose()


def test_upgrade_to_head_is_idempotent(app_db_url: str) -> None:
    upgrade_to_head(app_db_url)
    upgrade_to_head(app_db_url)
