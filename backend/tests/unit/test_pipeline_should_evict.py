"""Unit test for `app.core.pipeline._should_evict` -- the pure decision of
whether a SQL-tier cache hit's execution failure means the cached statement
is stale and worth evicting (see `Text2SQLPipeline._sql_hit`'s module
docstring section, "Two-tier cache").
"""

from __future__ import annotations

import pytest

from app.core.pipeline import _should_evict
from app.core.sql.executor import ExecutionError


@pytest.mark.parametrize("kind", ["syntax", "other"])
def test_repairable_kinds_are_evicted(kind: str) -> None:
    """Same kinds `_full_path`/`_repair` would spend a repair attempt on for
    a freshly-generated query -- plausibly means the cached SQL no longer
    matches the current schema."""
    assert _should_evict(ExecutionError(kind, "boom")) is True


@pytest.mark.parametrize("kind", ["timeout", "permission"])
def test_environmental_kinds_are_not_evicted(kind: str) -> None:
    """A fresh generation would hit the same statement timeout / read-only
    role wall identically, so the cached entry is not at fault."""
    assert _should_evict(ExecutionError(kind, "boom")) is False
