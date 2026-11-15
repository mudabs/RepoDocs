# SmartCart database fixture

Flyway style PostgreSQL migrations used by RepoDocs tests: three versioned
migrations that create, alter, rename, and drop tables, a schema-qualified table,
composite and implicit foreign keys, a partitioned table, and constructs the
adapter must report instead of analyzing (a function, a trigger, a view, an
unsupported ALTER action, a foreign key to an undefined table, an undo migration,
and a Liquibase changelog). A report query file holds no schema and is ignored.
