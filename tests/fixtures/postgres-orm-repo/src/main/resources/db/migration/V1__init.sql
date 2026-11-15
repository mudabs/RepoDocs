CREATE TABLE customers (
    id BIGSERIAL PRIMARY KEY,
    email_address VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE orders (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    total_amount NUMERIC(12, 2) NOT NULL
);

CREATE TABLE order_items (
    order_id BIGINT NOT NULL REFERENCES orders (id),
    line_no INTEGER NOT NULL,
    sku TEXT NOT NULL,
    PRIMARY KEY (order_id, line_no)
);
