-- SmartCart initial schema. A comment mentioning CREATE TABLE ignored_table must not count.
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE SCHEMA IF NOT EXISTS billing;

CREATE TYPE order_status AS ENUM ('OPEN', 'COMPLETED', 'ARCHIVED');

CREATE TABLE customers (
    id BIGSERIAL PRIMARY KEY,
    email_address VARCHAR(255) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

/* Orders reference customers; the status uses the enum above. */
CREATE TABLE IF NOT EXISTS orders (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    customer_id BIGINT NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    status order_status NOT NULL DEFAULT 'OPEN',
    total_amount NUMERIC(12, 2) NOT NULL CHECK (total_amount >= 0),
    notes TEXT,
    CONSTRAINT orders_pkey PRIMARY KEY (id)
);

CREATE INDEX idx_orders_customer ON orders (customer_id);
