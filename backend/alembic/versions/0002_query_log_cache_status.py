"""query_log.cache_status

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable on purpose: rows written before this migration have no
    # recorded outcome, and inventing one ("miss") would be a lie. Every
    # row app.db.query_log.record_query writes from now on sets it.
    op.add_column("query_log", sa.Column("cache_status", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("query_log", "cache_status")
