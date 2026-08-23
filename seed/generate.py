#!/usr/bin/env python3
"""Deterministic CSV generator for the NorthwindNext seed dataset.

Uses only the standard library plus `faker`. Writes CSVs to `seed/data/`
(relative to this script's own location) so it can be invoked from any
working directory, both at Docker image build time and from tests.

Determinism: a fixed RNG seed (42) and a fixed anchor date (2026-08-01,
NOT `date.today()`) are used so that row counts, ids and eval answers are
reproducible across machines and over time.
"""

from __future__ import annotations

import csv
import random
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from faker import Faker

SEED = 42
ANCHOR_DATE = date(2026, 8, 1)
DATA_DIR = Path(__file__).resolve().parent / "data"

N_CUSTOMERS = 2_000
N_ADDRESSES = 2_500
N_CATEGORIES = 20
N_SUPPLIERS = 40
N_PRODUCTS = 500
N_ORDERS = 20_000
N_REVIEWS = 8_000

SEGMENTS = ["retail", "wholesale", "vip"]
COUNTRIES = ["US", "CA", "GB", "DE", "FR", "AU", "JP", "BR", "IN", "MX"]
PAYMENT_METHODS = ["credit_card", "paypal", "bank_transfer", "gift_card"]
CARRIERS = ["ups", "fedex", "usps", "dhl"]

# Order status distribution: fixed percentages so cancelled/refunded rates
# match the seed contract exactly (10% cancelled, 3% refunded); the rest is
# split between pending/paid/shipped, biased toward shipped so that
# payments/shipments (which only exist for a subset of statuses) still end
# up with realistic-sized tables.
ORDER_STATUSES = ["pending", "paid", "shipped", "cancelled", "refunded"]
ORDER_STATUS_WEIGHTS = [3, 7, 77, 10, 3]

random.seed(SEED)
fake = Faker()
Faker.seed(SEED)


def to_utc_datetime(d: date, t: time | None = None) -> datetime:
    t = t or time(
        hour=random.randint(0, 23), minute=random.randint(0, 59), second=random.randint(0, 59)
    )
    return datetime.combine(d, t, tzinfo=UTC)


def random_datetime_between(start: date, end: date) -> datetime:
    span_days = (end - start).days
    offset = random.randint(0, max(span_days, 0))
    return to_utc_datetime(start + timedelta(days=offset))


def write_csv(name: str, header: list[str], rows: list[list[object]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def iso(dt: datetime | date | None) -> str:
    return "" if dt is None else dt.isoformat()


def main() -> None:
    order_window_start = ANCHOR_DATE - timedelta(days=730)  # trailing 24 months

    # -- customers ---------------------------------------------------------
    customers = []
    for cid in range(1, N_CUSTOMERS + 1):
        signup = order_window_start + timedelta(
            days=random.randint(0, (ANCHOR_DATE - order_window_start).days)
        )
        customers.append(
            {
                "id": cid,
                "first_name": fake.first_name(),
                "last_name": fake.last_name(),
                "email": f"{fake.user_name()}.{cid}@example.com",
                "country": random.choice(COUNTRIES),
                "segment": random.choice(SEGMENTS),
                "signup_date": signup,
            }
        )
    write_csv(
        "customers",
        [
            "id",
            "first_name",
            "last_name",
            "email",
            "country",
            "segment",
            "signup_date",
            "created_at",
        ],
        [
            [
                c["id"],
                c["first_name"],
                c["last_name"],
                c["email"],
                c["country"],
                c["segment"],
                iso(c["signup_date"]),
                iso(to_utc_datetime(c["signup_date"])),
            ]
            for c in customers
        ],
    )

    # -- addresses -----------------------------------------------------------
    # Every customer gets at least one address; the remaining addresses are
    # distributed randomly to give some customers a second/third address.
    addresses: list[dict] = []
    customer_addresses: dict[int, list[int]] = {c["id"]: [] for c in customers}
    aid = 1
    for c in customers:
        addresses.append({"id": aid, "customer_id": c["id"], "is_default": True})
        customer_addresses[c["id"]].append(aid)
        aid += 1
    while aid <= N_ADDRESSES:
        cid = random.randint(1, N_CUSTOMERS)
        addresses.append({"id": aid, "customer_id": cid, "is_default": False})
        customer_addresses[cid].append(aid)
        aid += 1

    def address_row(a: dict) -> list[object]:
        return [
            a["id"],
            a["customer_id"],
            fake.street_address(),
            "",
            fake.city(),
            fake.state(),
            fake.postcode(),
            random.choice(COUNTRIES),
            a["is_default"],
        ]

    write_csv(
        "addresses",
        [
            "id",
            "customer_id",
            "line1",
            "line2",
            "city",
            "state",
            "postal_code",
            "country",
            "is_default",
        ],
        [address_row(a) for a in addresses],
    )

    # -- categories (self-referencing hierarchy) -----------------------------
    categories = []
    n_top_level = 5
    for i in range(1, N_CATEGORIES + 1):
        parent_id = None if i <= n_top_level else random.randint(1, i - 1)
        categories.append(
            {"id": i, "name": f"{fake.word().capitalize()} {i}", "parent_id": parent_id}
        )
    write_csv(
        "categories",
        ["id", "name", "parent_id"],
        [
            [c["id"], c["name"], c["parent_id"] if c["parent_id"] is not None else ""]
            for c in categories
        ],
    )

    # -- suppliers ------------------------------------------------------------
    suppliers = [
        {
            "id": i,
            "name": fake.company(),
            "country": random.choice(COUNTRIES),
            "contact_email": fake.company_email(),
        }
        for i in range(1, N_SUPPLIERS + 1)
    ]
    write_csv(
        "suppliers",
        ["id", "name", "country", "contact_email"],
        [[s["id"], s["name"], s["country"], s["contact_email"]] for s in suppliers],
    )

    # -- products ---------------------------------------------------------------
    products = []
    for i in range(1, N_PRODUCTS + 1):
        price = round(random.uniform(5, 500), 2)
        discontinued_at = None
        if random.random() < 0.08:  # ~8% of products discontinued
            discontinued_at = random_datetime_between(order_window_start, ANCHOR_DATE)
        products.append(
            {
                "id": i,
                "sku": f"SKU-{i:05d}",
                "name": fake.catch_phrase(),
                "category_id": random.randint(1, N_CATEGORIES),
                "supplier_id": random.randint(1, N_SUPPLIERS),
                "unit_price": price,
                "discontinued_at": discontinued_at,
            }
        )
    write_csv(
        "products",
        ["id", "sku", "name", "category_id", "supplier_id", "unit_price", "discontinued_at"],
        [
            [
                p["id"],
                p["sku"],
                p["name"],
                p["category_id"],
                p["supplier_id"],
                p["unit_price"],
                iso(p["discontinued_at"]),
            ]
            for p in products
        ],
    )

    # -- inventory ----------------------------------------------------------
    inventory_rows = []
    for p in products:
        reorder_level = random.randint(5, 50)
        qty = random.randint(0, 500)
        inventory_rows.append([p["id"], p["id"], qty, reorder_level, f"WH-{random.randint(1, 6)}"])
    write_csv(
        "inventory",
        ["id", "product_id", "quantity_on_hand", "reorder_level", "warehouse_location"],
        inventory_rows,
    )

    # -- orders ---------------------------------------------------------------
    orders = []
    for i in range(1, N_ORDERS + 1):
        cid = random.randint(1, N_CUSTOMERS)
        addr_id = random.choice(customer_addresses[cid])
        order_dt = random_datetime_between(order_window_start, ANCHOR_DATE)
        status = random.choices(ORDER_STATUSES, weights=ORDER_STATUS_WEIGHTS, k=1)[0]
        orders.append(
            {
                "id": i,
                "customer_id": cid,
                "shipping_address_id": addr_id,
                "status": status,
                "order_date": order_dt,
            }
        )
    write_csv(
        "orders",
        ["id", "customer_id", "shipping_address_id", "status", "order_date", "created_at"],
        [
            [
                o["id"],
                o["customer_id"],
                o["shipping_address_id"],
                o["status"],
                iso(o["order_date"]),
                iso(o["order_date"]),
            ]
            for o in orders
        ],
    )

    # -- order_items ------------------------------------------------------------
    order_items_rows = []
    order_totals: dict[int, float] = {}
    oi_id = 1
    for o in orders:
        n_items = random.randint(1, 5)
        total = 0.0
        for _ in range(n_items):
            product = products[random.randint(0, N_PRODUCTS - 1)]
            jitter = random.uniform(-0.05, 0.05)
            unit_price = round(max(product["unit_price"] * (1 + jitter), 0.01), 2)
            quantity = random.randint(1, 4)
            order_items_rows.append([oi_id, o["id"], product["id"], quantity, unit_price])
            total += unit_price * quantity
            oi_id += 1
        order_totals[o["id"]] = round(total, 2)
    write_csv(
        "order_items",
        ["id", "order_id", "product_id", "quantity", "unit_price"],
        order_items_rows,
    )

    # -- payments (only for paid/shipped/refunded orders) ---------------------
    payments_rows = []
    pid = 1
    for o in orders:
        if o["status"] not in ("paid", "shipped", "refunded"):
            continue
        captured_at = o["order_date"] + timedelta(hours=random.randint(1, 48))
        payments_rows.append(
            [pid, o["id"], random.choice(PAYMENT_METHODS), order_totals[o["id"]], iso(captured_at)]
        )
        pid += 1
    write_csv(
        "payments",
        ["id", "order_id", "method", "amount", "captured_at"],
        payments_rows,
    )

    # -- shipments (only for shipped orders) -----------------------------------
    shipments_rows = []
    sid = 1
    for o in orders:
        if o["status"] != "shipped":
            continue
        shipped_at = o["order_date"] + timedelta(hours=random.randint(24, 96))
        delivered_at = None
        if random.random() < 0.9:  # 90% delivered, rest still in transit
            delivered_at = shipped_at + timedelta(hours=random.randint(12, 240))
        shipments_rows.append(
            [sid, o["id"], random.choice(CARRIERS), iso(shipped_at), iso(delivered_at)]
        )
        sid += 1
    write_csv(
        "shipments",
        ["id", "order_id", "carrier", "shipped_at", "delivered_at"],
        shipments_rows,
    )

    # -- reviews ----------------------------------------------------------------
    reviews_rows = []
    for i in range(1, N_REVIEWS + 1):
        product = products[random.randint(0, N_PRODUCTS - 1)]
        customer = customers[random.randint(0, N_CUSTOMERS - 1)]
        created_at = random_datetime_between(order_window_start, ANCHOR_DATE)
        rating = random.choices([1, 2, 3, 4, 5], weights=[5, 5, 15, 35, 40], k=1)[0]
        reviews_rows.append(
            [i, product["id"], customer["id"], rating, fake.sentence(nb_words=12), iso(created_at)]
        )
    write_csv(
        "reviews",
        ["id", "product_id", "customer_id", "rating", "comment", "created_at"],
        reviews_rows,
    )

    print(f"Wrote CSVs to {DATA_DIR}")


if __name__ == "__main__":
    main()
