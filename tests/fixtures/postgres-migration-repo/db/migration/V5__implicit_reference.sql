-- A foreign key that references the primary key without naming it depends on that key.
CREATE TABLE implicit_parent (id BIGSERIAL PRIMARY KEY, label TEXT);
CREATE TABLE implicit_child (id BIGSERIAL PRIMARY KEY, parent_id BIGINT REFERENCES implicit_parent);
ALTER TABLE implicit_parent DROP COLUMN id;
