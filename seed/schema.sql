-- NorthwindNext e-commerce schema.
-- Applied to the `target` database only. Runtime access to `target` is via
-- the readonly role defined in roles.sql.

CREATE TYPE order_status AS ENUM ('pending', 'paid', 'shipped', 'cancelled', 'refunded');

-- ---------------------------------------------------------------------------
-- customers
-- ---------------------------------------------------------------------------
CREATE TABLE customers (
    id            BIGSERIAL PRIMARY KEY,
    first_name    TEXT NOT NULL,
    last_name     TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    country       TEXT NOT NULL,
    segment       TEXT NOT NULL,
    signup_date   DATE NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE customers IS 'End customers who place orders.';
COMMENT ON COLUMN customers.segment IS 'Marketing segment, e.g. retail, wholesale, vip.';
COMMENT ON COLUMN customers.signup_date IS 'Date the customer created their account.';

-- ---------------------------------------------------------------------------
-- addresses
-- ---------------------------------------------------------------------------
CREATE TABLE addresses (
    id            BIGSERIAL PRIMARY KEY,
    customer_id   BIGINT NOT NULL REFERENCES customers (id),
    line1         TEXT NOT NULL,
    line2         TEXT,
    city          TEXT NOT NULL,
    state         TEXT,
    postal_code   TEXT NOT NULL,
    country       TEXT NOT NULL,
    is_default    BOOLEAN NOT NULL DEFAULT false
);

COMMENT ON TABLE addresses IS 'Shipping/billing addresses; a customer may have several (1:N).';
COMMENT ON COLUMN addresses.is_default IS 'True for the customer''s default address.';

CREATE INDEX ix_addresses_customer_id ON addresses (customer_id);

-- ---------------------------------------------------------------------------
-- categories
-- ---------------------------------------------------------------------------
CREATE TABLE categories (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    parent_id     BIGINT REFERENCES categories (id)
);

COMMENT ON TABLE categories IS 'Product categories; self-referencing hierarchy via parent_id.';
COMMENT ON COLUMN categories.parent_id IS 'Parent category id, NULL for a top-level category.';

CREATE INDEX ix_categories_parent_id ON categories (parent_id);

-- ---------------------------------------------------------------------------
-- suppliers
-- ---------------------------------------------------------------------------
CREATE TABLE suppliers (
    id             BIGSERIAL PRIMARY KEY,
    name           TEXT NOT NULL,
    country        TEXT NOT NULL,
    contact_email  TEXT NOT NULL
);

COMMENT ON TABLE suppliers IS 'Vendors that supply products.';

-- ---------------------------------------------------------------------------
-- products
-- ---------------------------------------------------------------------------
CREATE TABLE products (
    id                BIGSERIAL PRIMARY KEY,
    sku               TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    category_id       BIGINT NOT NULL REFERENCES categories (id),
    supplier_id       BIGINT NOT NULL REFERENCES suppliers (id),
    unit_price        NUMERIC(10, 2) NOT NULL,
    discontinued_at   TIMESTAMPTZ
);

COMMENT ON TABLE products IS 'Sellable products.';
COMMENT ON COLUMN products.unit_price IS 'Current catalog price; not necessarily what was charged historically, see order_items.unit_price.';
COMMENT ON COLUMN products.discontinued_at IS 'NULL = active';

CREATE INDEX ix_products_category_id ON products (category_id);
CREATE INDEX ix_products_supplier_id ON products (supplier_id);

-- ---------------------------------------------------------------------------
-- inventory
-- ---------------------------------------------------------------------------
CREATE TABLE inventory (
    id                  BIGSERIAL PRIMARY KEY,
    product_id          BIGINT NOT NULL UNIQUE REFERENCES products (id),
    quantity_on_hand    INTEGER NOT NULL,
    reorder_level       INTEGER NOT NULL,
    warehouse_location  TEXT NOT NULL
);

COMMENT ON TABLE inventory IS 'Current stock level per product (1:1 with products).';
COMMENT ON COLUMN inventory.reorder_level IS 'Stock threshold below which the product should be reordered.';

CREATE INDEX ix_inventory_product_id ON inventory (product_id);

-- ---------------------------------------------------------------------------
-- orders
-- ---------------------------------------------------------------------------
CREATE TABLE orders (
    id                    BIGSERIAL PRIMARY KEY,
    customer_id           BIGINT NOT NULL REFERENCES customers (id),
    shipping_address_id   BIGINT NOT NULL REFERENCES addresses (id),
    status                order_status NOT NULL,
    order_date            TIMESTAMPTZ NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE orders IS 'Customer orders.';
COMMENT ON COLUMN orders.status IS 'Order lifecycle state: pending, paid, shipped, cancelled, refunded.';
COMMENT ON COLUMN orders.order_date IS 'When the order was placed by the customer.';

CREATE INDEX ix_orders_customer_id ON orders (customer_id);
CREATE INDEX ix_orders_shipping_address_id ON orders (shipping_address_id);
CREATE INDEX ix_orders_order_date ON orders (order_date);

-- ---------------------------------------------------------------------------
-- order_items
-- ---------------------------------------------------------------------------
CREATE TABLE order_items (
    id            BIGSERIAL PRIMARY KEY,
    order_id      BIGINT NOT NULL REFERENCES orders (id),
    product_id    BIGINT NOT NULL REFERENCES products (id),
    quantity      INTEGER NOT NULL CHECK (quantity > 0),
    unit_price    NUMERIC(10, 2) NOT NULL
);

COMMENT ON TABLE order_items IS 'Line items belonging to an order.';
COMMENT ON COLUMN order_items.unit_price IS 'price at time of order; do not use products.unit_price for revenue';

CREATE INDEX ix_order_items_order_id ON order_items (order_id);
CREATE INDEX ix_order_items_product_id ON order_items (product_id);

-- ---------------------------------------------------------------------------
-- payments
-- ---------------------------------------------------------------------------
CREATE TABLE payments (
    id            BIGSERIAL PRIMARY KEY,
    order_id      BIGINT NOT NULL UNIQUE REFERENCES orders (id),
    method        TEXT NOT NULL,
    amount        NUMERIC(10, 2) NOT NULL,
    captured_at   TIMESTAMPTZ NOT NULL
);

COMMENT ON TABLE payments IS 'Captured payment for an order; only exists for paid, shipped or refunded orders.';
COMMENT ON COLUMN payments.method IS 'Payment method, e.g. credit_card, paypal, bank_transfer, gift_card.';

CREATE INDEX ix_payments_order_id ON payments (order_id);

-- ---------------------------------------------------------------------------
-- shipments
-- ---------------------------------------------------------------------------
CREATE TABLE shipments (
    id             BIGSERIAL PRIMARY KEY,
    order_id       BIGINT NOT NULL UNIQUE REFERENCES orders (id),
    carrier        TEXT NOT NULL,
    shipped_at     TIMESTAMPTZ NOT NULL,
    delivered_at   TIMESTAMPTZ
);

COMMENT ON TABLE shipments IS 'Shipment for an order; only exists for orders with status shipped.';
COMMENT ON COLUMN shipments.delivered_at IS 'NULL = not yet delivered';

CREATE INDEX ix_shipments_order_id ON shipments (order_id);

-- ---------------------------------------------------------------------------
-- reviews
-- ---------------------------------------------------------------------------
CREATE TABLE reviews (
    id            BIGSERIAL PRIMARY KEY,
    product_id    BIGINT NOT NULL REFERENCES products (id),
    customer_id   BIGINT NOT NULL REFERENCES customers (id),
    rating        SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE reviews IS 'Customer product reviews.';
COMMENT ON COLUMN reviews.rating IS 'Star rating from 1 (worst) to 5 (best).';

CREATE INDEX ix_reviews_product_id ON reviews (product_id);
CREATE INDEX ix_reviews_customer_id ON reviews (customer_id);
