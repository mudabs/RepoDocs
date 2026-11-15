-- Repeating a definition must not duplicate it.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer BIGINT;
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders (customer);
