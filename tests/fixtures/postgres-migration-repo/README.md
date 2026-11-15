# SmartCart migration fixture

Five Flyway migrations that exercise how the adapter keeps schema state while
migrations run:

- `V1` creates the tables, their keys, and their indexes, including a table
  that a later migration drops.
- `V2` renames and drops columns that other keys and indexes depend on, and
  repeats a table definition.
- `V3` only changes the schema around the tables: it declares a schema and an
  extension, adds an index, drops a table, and drops an index.
- `V4` repeats a column and an index definition, which must not duplicate them.
- `V5` declares a foreign key that references the primary key without naming
  it, and then drops that key.

A nested block comment hides a table definition.
