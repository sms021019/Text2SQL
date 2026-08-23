from app.core.schema.models import Column, ForeignKey, SchemaGraph, Table
from app.core.schema.render import render_ddl


def _customers_table() -> Table:
    return Table(
        name="customers",
        comment="End customers who place orders.",
        columns=[
            Column(name="id", type="BIGINT", nullable=False, comment=None, is_pk=True),
            Column(name="email", type="TEXT", nullable=False, comment=None, is_pk=False),
        ],
        foreign_keys=[],
    )


def _orders_table(extra_column: bool = False) -> Table:
    columns = [
        Column(name="id", type="BIGINT", nullable=False, comment=None, is_pk=True),
        Column(name="customer_id", type="BIGINT", nullable=False, comment=None, is_pk=False),
        Column(
            name="status",
            type="order_status",
            nullable=False,
            comment=(
                "one of: pending, paid, shipped, cancelled, refunded. "
                "Order lifecycle state: pending, paid, shipped, cancelled, refunded."
            ),
            is_pk=False,
        ),
    ]
    if extra_column:
        columns.append(Column(name="note", type="TEXT", nullable=True, comment=None, is_pk=False))
    return Table(
        name="orders",
        comment="Customer orders.",
        columns=columns,
        foreign_keys=[ForeignKey(column="customer_id", ref_table="customers", ref_column="id")],
    )


def _graph(extra_column: bool = False) -> SchemaGraph:
    return SchemaGraph.build(
        tables={"customers": _customers_table(), "orders": _orders_table(extra_column)}
    )


def test_render_ddl_contains_expected_fragments() -> None:
    graph = _graph()
    ddl = render_ddl(graph, ["orders", "customers"])

    assert "CREATE TABLE orders (" in ddl
    assert "-- Customer orders." in ddl
    assert "id BIGINT NOT NULL, -- primary key" in ddl
    assert (
        "status order_status NOT NULL, -- one of: pending, paid, shipped, cancelled, "
        "refunded. Order lifecycle state:" in ddl
    )
    assert "PRIMARY KEY (id)" in ddl
    assert "FOREIGN KEY (customer_id) REFERENCES customers(id)" in ddl
    assert "CREATE TABLE customers (" in ddl

    # tables render in the order given by the `tables` argument
    assert ddl.index("CREATE TABLE orders") < ddl.index("CREATE TABLE customers")


def test_render_ddl_unknown_table_raises_key_error() -> None:
    graph = _graph()
    try:
        render_ddl(graph, ["does_not_exist"])
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for unknown table name")


def test_version_stable_across_constructions() -> None:
    g1 = _graph()
    g2 = _graph()
    assert g1.version == g2.version
    assert len(g1.version) == 12


def test_version_changes_when_column_added() -> None:
    g1 = _graph()
    g2 = _graph(extra_column=True)
    assert g1.version != g2.version


def test_neighbors_excludes_self_on_self_referencing_fk() -> None:
    categories = Table(
        name="categories",
        comment="Product categories; self-referencing hierarchy via parent_id.",
        columns=[
            Column(name="id", type="BIGINT", nullable=False, comment=None, is_pk=True),
            Column(
                name="parent_id",
                type="BIGINT",
                nullable=True,
                comment="Parent category id, NULL for a top-level category.",
                is_pk=False,
            ),
        ],
        foreign_keys=[ForeignKey(column="parent_id", ref_table="categories", ref_column="id")],
    )
    graph = SchemaGraph.build(tables={"categories": categories})

    assert "categories" not in graph.neighbors("categories")
    assert graph.neighbors("categories") == set()


def test_table_summary() -> None:
    orders = _orders_table()
    assert orders.summary() == ("orders: Customer orders. columns: id, customer_id, status")


def test_table_summary_without_comment() -> None:
    table = Table(
        name="widgets",
        comment=None,
        columns=[Column(name="id", type="BIGINT", nullable=False, comment=None, is_pk=True)],
        foreign_keys=[],
    )
    assert table.summary() == "widgets: columns: id"
