import psycopg
import pytest

pytestmark = pytest.mark.integration


def test_row_counts(target_db_url):
    with psycopg.connect(target_db_url) as c:
        assert c.execute("select count(*) from orders").fetchone()[0] == 20000
        assert c.execute("select count(*) from order_items").fetchone()[0] >= 50000


def test_readonly_role_cannot_write(target_db_url):
    ro = target_db_url.replace("postgres:postgres", "readonly:readonly")
    with psycopg.connect(ro) as c, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        c.execute("delete from reviews")


def test_readonly_role_can_select(target_db_url):
    ro = target_db_url.replace("postgres:postgres", "readonly:readonly")
    with psycopg.connect(ro) as c:
        assert c.execute("select count(*) from customers").fetchone()[0] == 2000


def test_order_items_trap_documented(target_db_url):
    """order_items.unit_price must carry the revenue-trap comment."""
    with psycopg.connect(target_db_url) as c:
        row = c.execute(
            """
            select col_description('order_items'::regclass, ordinal_position)
            from information_schema.columns
            where table_name = 'order_items' and column_name = 'unit_price'
            """
        ).fetchone()
        assert row is not None
        assert "do not use products.unit_price for revenue" in row[0]


def test_payments_only_for_paid_shipped_refunded(target_db_url):
    with psycopg.connect(target_db_url) as c:
        bad = c.execute(
            """
            select count(*) from payments p
            join orders o on o.id = p.order_id
            where o.status not in ('paid', 'shipped', 'refunded')
            """
        ).fetchone()[0]
        assert bad == 0


def test_shipments_only_for_shipped(target_db_url):
    with psycopg.connect(target_db_url) as c:
        bad = c.execute(
            """
            select count(*) from shipments s
            join orders o on o.id = s.order_id
            where o.status != 'shipped'
            """
        ).fetchone()[0]
        assert bad == 0
