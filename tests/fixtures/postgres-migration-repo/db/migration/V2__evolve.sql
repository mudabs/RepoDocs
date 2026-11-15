-- A repeated definition must not erase the columns, keys, and indexes of the table.
CREATE TABLE IF NOT EXISTS orders (
    id BIGINT PRIMARY KEY
);

/* A commented-out table must stay commented out.
   /* The inner comment ends here, the outer one does not. */
CREATE TABLE commented_out (id BIGINT PRIMARY KEY);
*/

ALTER TABLE customers RENAME COLUMN email TO email_address;
ALTER TABLE customers RENAME COLUMN legacy_code TO legacy;
ALTER TABLE customers RENAME COLUMN tenant TO tenant_id;
ALTER TABLE customers DROP COLUMN legacy;
ALTER TABLE customers DROP COLUMN code;

ALTER TABLE orders RENAME COLUMN customer_id TO customer;
ALTER TABLE orders DROP COLUMN reference;
