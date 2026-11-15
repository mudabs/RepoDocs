ALTER TABLE orders ADD COLUMN shipped_at TIMESTAMPTZ;
ALTER TABLE orders ALTER COLUMN notes SET NOT NULL, ALTER COLUMN total_amount TYPE NUMERIC(14, 2);
ALTER TABLE orders RENAME COLUMN notes TO comment;
ALTER TABLE orders ADD COLUMN warehouse_id BIGINT;
ALTER TABLE orders ADD CONSTRAINT fk_orders_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (id);
ALTER TABLE customers ADD CONSTRAINT customers_email_lower CHECK (email_address = lower(email_address));

CREATE TABLE temp_stuff (id INTEGER PRIMARY KEY);
DROP TABLE IF EXISTS temp_stuff CASCADE;

CREATE TABLE audit_log (id BIGSERIAL PRIMARY KEY, message TEXT);
ALTER TABLE audit_log RENAME TO audit_entries;

ALTER TABLE orders SET (fillfactor = 70);

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now(); -- a semicolon inside the body; must not split the statement
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER orders_touch BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE VIEW open_orders AS SELECT * FROM orders WHERE status = 'OPEN';

ALTER TABLE missing_table ADD COLUMN x INTEGER;
