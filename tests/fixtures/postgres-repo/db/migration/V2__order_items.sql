CREATE TABLE order_items (
    order_id BIGINT NOT NULL,
    line_no INTEGER NOT NULL,
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_price NUMERIC(12, 2) NOT NULL,
    PRIMARY KEY (order_id, line_no),
    CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE ON UPDATE NO ACTION
);

CREATE TABLE billing.invoices (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id BIGINT UNIQUE REFERENCES orders,
    "IssuedAt" TIMESTAMPTZ NOT NULL,
    payload JSONB
);

CREATE TABLE events (
    occurred_at TIMESTAMPTZ NOT NULL,
    kind TEXT NOT NULL,
    body JSONB
) PARTITION BY RANGE (occurred_at);

CREATE TABLE events_2024 PARTITION OF events FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');

INSERT INTO customers (email_address) VALUES ('seed@example.invalid; not a statement break');
