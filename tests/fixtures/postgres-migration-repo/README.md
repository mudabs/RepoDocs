# SmartCart migration fixture

Two Flyway migrations that exercise how the adapter keeps schema state while
migrations run: a repeated `CREATE TABLE IF NOT EXISTS` that must not erase the
columns, keys, and indexes of the table, renames and drops of columns that other
keys and indexes depend on, and a nested block comment that hides a table
definition.
