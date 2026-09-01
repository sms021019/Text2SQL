"""SQLAlchemy ORM models for the app's own database (as opposed to the
read-only `target` database the pipeline queries against).

Currently just `QueryLog`, one row per pipeline run -- see
`app/db/query_log.py` for how a row gets written.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

__all__ = ["Base", "QueryLog"]


class Base(DeclarativeBase):
    # Plain `Mapped[str]` would default to `String` (VARCHAR); every text
    # column in this schema is unbounded, so map `str`/`str | None` to
    # `Text` here instead of spelling `mapped_column(Text)` on each field.
    type_annotation_map = {str: Text}


class QueryLog(Base):
    """One row per `Text2SQLPipeline.run()` call, successful or not."""

    __tablename__ = "query_log"
    __table_args__ = (Index("ix_query_log_created_at", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    question: Mapped[str]
    sql: Mapped[str | None]
    tables: Mapped[list[str]] = mapped_column(JSONB)
    success: Mapped[bool]
    error: Mapped[str | None]
    repaired: Mapped[bool]
    latency_ms: Mapped[float]
    prompt_tokens: Mapped[int]
    completion_tokens: Mapped[int]
    model: Mapped[str]
    schema_version: Mapped[str]
    request_id: Mapped[str]
    #: PipelineOutput.cache_status for this run (miss | sql_hit | result_hit |
    #: bypass | disabled). NULL only on rows older than migration 0002.
    cache_status: Mapped[str | None]
