-- A migration that changes the schema around the tables.
CREATE SCHEMA reporting;
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE INDEX idx_customers_email ON customers (email_address);
DROP TABLE legacy_orders;
DROP INDEX idx_customers_tenant;
