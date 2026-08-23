import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.schema.introspect import introspect
from app.core.schema.render import render_ddl

pytestmark = pytest.mark.integration


async def test_introspect_builds_schema_graph(readonly_async_url: str) -> None:
    engine = create_async_engine(readonly_async_url)
    try:
        graph = await introspect(engine)
    finally:
        await engine.dispose()

    assert len(graph.tables) == 11
    assert set(graph.tables) == {
        "customers",
        "addresses",
        "categories",
        "suppliers",
        "products",
        "inventory",
        "orders",
        "order_items",
        "payments",
        "shipments",
        "reviews",
    }

    orders = graph.tables["orders"]
    assert any(fk.ref_table == "customers" and fk.ref_column == "id" for fk in orders.foreign_keys)

    order_items = graph.tables["order_items"]
    unit_price = next(c for c in order_items.columns if c.name == "unit_price")
    assert unit_price.comment is not None
    assert "price at time of order" in unit_price.comment

    neighbors = graph.neighbors("orders")
    assert {"customers", "order_items", "payments", "shipments", "addresses"} <= neighbors

    # version is a deterministic sha256 prefix
    assert len(graph.version) == 12
    assert graph.version == graph.version.lower()


async def test_introspect_orders_status_enum_rendered(readonly_async_url: str) -> None:
    engine = create_async_engine(readonly_async_url)
    try:
        graph = await introspect(engine)
    finally:
        await engine.dispose()

    status = next(c for c in graph.tables["orders"].columns if c.name == "status")
    assert status.comment is not None
    assert "one of: pending, paid, shipped, cancelled, refunded" in status.comment

    ddl = render_ddl(graph, ["orders"])
    assert "one of: pending, paid, shipped, cancelled, refunded" in ddl
