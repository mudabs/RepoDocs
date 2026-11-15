CREATE TABLE customers (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    legacy_code TEXT,
    tenant TEXT NOT NULL,
    code TEXT,
    CONSTRAINT customers_tenant_uq UNIQUE (tenant),
    CONSTRAINT customers_code_uq UNIQUE (code)
);

CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers (id),
    reference TEXT
);

CREATE INDEX idx_orders_customer ON orders (customer_id);
CREATE UNIQUE INDEX idx_orders_reference ON orders (reference);
