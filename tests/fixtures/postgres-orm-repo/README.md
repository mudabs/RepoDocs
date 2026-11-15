# SmartCart store fixture

A Spring Boot repository that owns its PostgreSQL schema: JPA entities and the
Flyway migration that creates the tables live in the same repository. The
fixture exists to check the ORM metadata path of the PostgreSQL adapter, so
`Order` declares `@Table(name = "orders")` while `Customer` declares no table
name, and `order_items` has no entity at all. A link may only be inferred for
the entity whose declared table name matches exactly one extracted table.
