"""PostgreSQL static schema adapter.

Reads SQL schema and migration files and applies the data-definition statements
it supports, in migration order, to build the resulting tables: columns, types,
nullability, defaults, primary keys, unique constraints, foreign keys, and
indexes, each with evidence pointing at the statements that defined them.
Foreign keys between tables defined in the same repository become `references`
edges. Everything else (views, functions, triggers, procedural blocks, table
inheritance, Liquibase changelogs, unsupported ALTER actions, references to
tables that are not defined in the scanned SQL) is reported as a limitation.
No database is contacted and no SQL is executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..ir import claim_id, node_id, relationship_id
from . import (
    CLASSIFICATION_OBSERVED,
    LIMITATION_AMBIGUOUS_REFERENCE,
    LIMITATION_UNREADABLE_SOURCE,
    LIMITATION_UNSUPPORTED_CONSTRUCT,
    AnalysisResult,
    RepositoryContext,
    SourceFile,
)
from .sql import SqlFile, Statement, Token, identifier_name, paren_group, parse_sql, split_items


# PostgreSQL-only constructs that make a file recognizably PostgreSQL rather than
# portable SQL. Types are matched against the type a column was declared with, so a
# column that happens to be named like a type is not mistaken for one.
POSTGRESQL_TYPES = {
    "bigserial",
    "bytea",
    "cidr",
    "inet",
    "interval",
    "jsonb",
    "macaddr",
    "money",
    "serial",
    "smallserial",
    "timestamptz",
    "timetz",
    "tsquery",
    "tsvector",
    "uuid",
    "xml",
}
COLUMN_CONSTRAINT_WORDS = {"NOT", "NULL", "DEFAULT", "PRIMARY", "REFERENCES", "UNIQUE", "CHECK", "GENERATED", "CONSTRAINT", "COLLATE"}
TABLE_CONSTRAINT_WORDS = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "EXCLUDE", "LIKE"}
# Statements that carry no schema the adapter models. Data statements are counted
# separately, and the CREATE and DROP forms are handled where they are applied.
IGNORED_HEADS = (
    ("ALTER", "SEQUENCE"),
    ("ALTER", "TYPE"),
    ("ALTER", "ROLE"),
    ("COMMENT",),
    ("GRANT",),
    ("REVOKE",),
    ("SET",),
    ("RESET",),
    ("BEGIN",),
    ("COMMIT",),
    ("END",),
    ("START",),
    ("ANALYZE",),
    ("VACUUM",),
    ("CLUSTER",),
    ("LOCK",),
)
DATA_HEADS = {"INSERT", "UPDATE", "DELETE", "SELECT", "TRUNCATE"}
CREATE_MODIFIERS = ("OR", "REPLACE", "UNLOGGED", "TEMP", "TEMPORARY", "GLOBAL", "LOCAL", "UNIQUE", "MATERIALIZED")
FLYWAY_VERSIONED = re.compile(r"^V(?P<version>[0-9][0-9_.]*?)__(?P<description>.+)\.sql$", re.IGNORECASE)
FLYWAY_REPEATABLE = re.compile(r"^R__.+\.sql$", re.IGNORECASE)
FLYWAY_UNDO = re.compile(r"^U[0-9][0-9_.]*__.+\.sql$", re.IGNORECASE)
LIQUIBASE_FILE = re.compile(r"(^|/)[^/]*changelog[^/]*\.(xml|ya?ml|json)$", re.IGNORECASE)


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    default: str | None = None
    primary_key: bool = False
    unique: bool = False
    identity: bool = False
    generated: bool = False
    line: int = 0
    path: str = ""


@dataclass
class ForeignKey:
    name: str | None
    columns: list[str]
    references_table: str
    references_columns: list[str]
    on_delete: str | None
    on_update: str | None
    evidence_id: str
    implicit_target_columns: bool = False


@dataclass
class Table:
    schema: str | None
    name: str
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    primary_key_name: str | None = None
    unique_constraints: list[dict[str, Any]] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    checks: list[str | None] = field(default_factory=list)
    indexes: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    defined_in: list[str] = field(default_factory=list)
    partition_of: str | None = None
    created_line: int = 0
    created_path: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema and self.schema != "public" else self.name

    def column(self, name: str) -> Column | None:
        return next((item for item in self.columns if item.name == name), None)


class StatementError(Exception):
    """A statement form the adapter recognizes but cannot parse."""


class PostgreSqlAnalyzer:
    name = "postgresql"
    version = "1.0"

    def __init__(self) -> None:
        self._parse_cache: dict[tuple[str, str, str], SqlFile | None] = {}

    # ----------------------------------------------------------------- detection
    def detect(self, context: RepositoryContext) -> bool:
        if any(LIQUIBASE_FILE.search(source.path) for source in context.files):
            return True
        for source in self._sql_files(context):
            parsed = self._parsed(context, source)
            if parsed is None:
                # The file may hold a schema the adapter cannot read; report it.
                return True
            if any(_creates_table(statement) for statement in parsed.statements):
                return True
        return False

    @staticmethod
    def _sql_files(context: RepositoryContext) -> list[SourceFile]:
        """SQL files that may define a schema; Flyway undo migrations are excluded."""

        return [
            source
            for source in context.files_with_suffix(".sql")
            if FLYWAY_UNDO.match(source.path.rsplit("/", 1)[-1]) is None
        ]

    def _parsed(self, context: RepositoryContext, source: SourceFile) -> SqlFile | None:
        key = (context.repository_id, source.path, source.content_hash)
        if key not in self._parse_cache:
            text = context.text(source.path)
            self._parse_cache[key] = None if text is None else parse_sql(source.path, text)
        return self._parse_cache[key]

    # ------------------------------------------------------------------ analysis
    def analyze(self, context: RepositoryContext) -> AnalysisResult:
        result = AnalysisResult()
        repository = context.repository_id
        sql_files = sorted(self._sql_files(context), key=lambda source: migration_order(source.path))
        parsed_files: dict[str, SqlFile] = {}
        for source in sql_files:
            parsed = self._parsed(context, source)
            if parsed is None:
                evidence = context.file_evidence(source.path)
                result.evidence.append(evidence)
                result.limitations.append(
                    context.limitation(
                        LIMITATION_UNREADABLE_SOURCE,
                        f"{source.path} could not be decoded as UTF-8 or changed during the scan.",
                        [evidence["id"]],
                    )
                )
                continue
            parsed_files[source.path] = parsed
        schema_files = [
            source
            for source in sql_files
            if source.path in parsed_files
            and any(_creates_table(statement) for statement in parsed_files[source.path].statements)
        ]
        liquibase = [source for source in context.files if LIQUIBASE_FILE.search(source.path)]
        if not schema_files and not liquibase:
            return result

        tables: dict[str, Table] = {}
        schemas: set[str] = set()
        extensions: set[str] = set()
        enum_types: dict[str, list[str]] = {}
        dialect_markers: set[str] = set()
        data_statements = 0
        file_evidence_ids: list[str] = []
        for source in schema_files:
            parsed = parsed_files[source.path]
            evidence = context.file_evidence(source.path)
            result.evidence.append(evidence)
            file_evidence_ids.append(evidence["id"])
            for statement in parsed.statements:
                span = context.span_evidence(
                    source.path, parsed.line(statement.start), parsed.line(max(statement.end - 1, statement.start))
                )
                try:
                    data_statements += self._apply(
                        context, parsed, statement, span, tables, schemas, extensions, enum_types, dialect_markers, result
                    )
                except StatementError as exc:
                    result.evidence.append(span)
                    result.limitations.append(
                        context.limitation(
                            LIMITATION_UNSUPPORTED_CONSTRUCT,
                            f"{source.path} line {parsed.line(statement.start)}: {exc}; the statement was not applied.",
                            [span["id"]],
                        )
                    )
        for source in liquibase:
            evidence = context.file_evidence(source.path)
            result.evidence.append(evidence)
            result.limitations.append(
                context.limitation(
                    LIMITATION_UNSUPPORTED_CONSTRUCT,
                    f"{source.path} is a Liquibase changelog; changelogs are not analyzed, only SQL files are.",
                    [evidence["id"]],
                )
            )
        undo_files = [source for source in context.files_with_suffix(".sql") if FLYWAY_UNDO.match(source.path.rsplit("/", 1)[-1])]
        if undo_files:
            evidence = [context.file_evidence(source.path) for source in undo_files]
            result.evidence.extend(evidence)
            result.limitations.append(
                context.limitation(
                    LIMITATION_UNSUPPORTED_CONSTRUCT,
                    f"{len(undo_files)} Flyway undo migration(s) were not applied; the schema reflects versioned and repeatable migrations only.",
                    [item["id"] for item in evidence],
                )
            )
        if not schema_files:
            # Nothing was extracted, so there is no database fact to record. The
            # limitations above explain why.
            return result

        database_node = node_id("database", repository, "postgresql")
        migration_tool = "flyway" if any(FLYWAY_VERSIONED.match(s.path.rsplit("/", 1)[-1]) for s in schema_files) else "sql-files"
        result.nodes.append(
            {
                "id": database_node,
                "type": "database",
                "name": f"{repository} database",
                "attributes": {
                    "repository_id": repository,
                    "engine": "postgresql",
                    "dialect": "postgresql",
                    "dialect_markers": sorted(dialect_markers),
                    "migration_tool": migration_tool,
                    "sql_files": [source.path for source in schema_files],
                    "schemas": sorted(schemas),
                    "extensions": sorted(extensions),
                    "enum_types": {name: values for name, values in sorted(enum_types.items())},
                    "table_count": len(tables),
                    "data_statements_skipped": data_statements,
                },
                "evidence_ids": sorted(file_evidence_ids),
            }
        )
        result.relationships.append(self._edge("contains", context.repository_node_id, database_node, sorted(file_evidence_ids)))
        result.claims.append(
            self._claim(
                f"Repository {repository} defines {len(tables)} table(s) in {len(schema_files)} SQL file(s) applied in {migration_tool} order.",
                sorted(file_evidence_ids),
            )
        )

        table_nodes: dict[str, str] = {}
        for key, table in sorted(tables.items()):
            table_node = node_id("table", repository, key)
            table_nodes[key] = table_node
            evidence_ids = sorted(set(table.evidence_ids))
            result.nodes.append(
                {
                    "id": table_node,
                    "type": "table",
                    "name": table.qualified,
                    "attributes": {
                        "repository_id": repository,
                        "schema": table.schema,
                        "table": table.name,
                        "columns": [
                            {
                                "name": column.name,
                                "type": column.type,
                                "nullable": column.nullable,
                                "default": column.default,
                                "primary_key": column.name in table.primary_key,
                                "unique": column.unique or any(uc["columns"] == [column.name] for uc in table.unique_constraints),
                                "identity": column.identity,
                                "generated": column.generated,
                                "line": column.line,
                            }
                            for column in table.columns
                        ],
                        "primary_key": table.primary_key,
                        "unique_constraints": table.unique_constraints,
                        "foreign_keys": [
                            {
                                "name": fk.name,
                                "columns": fk.columns,
                                "references_table": fk.references_table,
                                "references_columns": fk.references_columns,
                                "implicit_target_columns": fk.implicit_target_columns,
                                "on_delete": fk.on_delete,
                                "on_update": fk.on_update,
                            }
                            for fk in table.foreign_keys
                        ],
                        "check_constraints": len(table.checks),
                        "indexes": table.indexes,
                        "partition_of": table.partition_of,
                        "defined_in": sorted(set(table.defined_in)),
                    },
                    "evidence_ids": evidence_ids,
                }
            )
            result.relationships.append(self._edge("contains", database_node, table_node, evidence_ids))
            result.claims.append(
                self._claim(
                    f"Table {table.qualified} has {len(table.columns)} column(s) and primary key ({', '.join(table.primary_key) or 'none'}) in repository {repository}.",
                    evidence_ids,
                )
            )
        for key, table in sorted(tables.items()):
            for fk in table.foreign_keys:
                target_key = fk.references_table
                if target_key not in tables:
                    result.limitations.append(
                        context.limitation(
                            LIMITATION_AMBIGUOUS_REFERENCE,
                            f"{table.qualified}.{', '.join(fk.columns)} references {fk.references_table}, which is not defined in the scanned SQL; no edge was created.",
                            [fk.evidence_id],
                        )
                    )
                    continue
                target = tables[target_key]
                result.relationships.append(
                    self._edge(
                        "references",
                        table_nodes[key],
                        table_nodes[target_key],
                        [fk.evidence_id],
                        {
                            "constraint": fk.name,
                            "columns": fk.columns,
                            "references_columns": fk.references_columns,
                            "on_delete": fk.on_delete,
                            "on_update": fk.on_update,
                        },
                    )
                )
                result.claims.append(
                    self._claim(
                        f"{table.qualified}.{', '.join(fk.columns)} references {target.qualified}.{', '.join(fk.references_columns) or 'primary key'}.",
                        [fk.evidence_id],
                    )
                )
        return result

    # ------------------------------------------------------------ helpers: IR
    @staticmethod
    def _edge(
        edge_type: str, source: str, target: str, evidence_ids: list[str], attributes: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        edge: dict[str, Any] = {
            "id": relationship_id(edge_type, source, target),
            "type": edge_type,
            "source": source,
            "target": target,
            "classification": CLASSIFICATION_OBSERVED,
            "confidence": 1.0,
            "evidence_ids": sorted(set(evidence_ids)),
        }
        if attributes:
            edge["attributes"] = attributes
        return edge

    @staticmethod
    def _claim(text: str, evidence_ids: list[str]) -> dict[str, Any]:
        ordered = sorted(set(evidence_ids))
        return {
            "id": claim_id(text, ordered),
            "text": text,
            "classification": CLASSIFICATION_OBSERVED,
            "confidence": 1.0,
            "evidence_ids": ordered,
        }

    # ------------------------------------------------------- statement handling
    def _apply(
        self,
        context: RepositoryContext,
        parsed: SqlFile,
        statement: Statement,
        span: dict[str, Any],
        tables: dict[str, Table],
        schemas: set[str],
        extensions: set[str],
        enum_types: dict[str, list[str]],
        dialect_markers: set[str],
        result: AnalysisResult,
    ) -> int:
        """Apply one statement; returns 1 when it was a skipped data statement, else 0."""

        tokens = statement.tokens
        if not tokens:
            return 0
        head = tokens[0].upper
        words = [token.upper for token in tokens[:6]]
        if head == "CREATE":
            index = 1
            while index < len(tokens) and tokens[index].is_word(*CREATE_MODIFIERS):
                index += 1
            kind = tokens[index].upper if index < len(tokens) else ""
            if kind == "TABLE":
                self._create_table(context, parsed, tokens, index + 1, span, tables, dialect_markers, result)
                return 0
            if kind == "INDEX":
                self._create_index(parsed, tokens, index + 1, span, tables, result)
                return 0
            if kind == "SCHEMA":
                name_index = _skip_if_not_exists(tokens, index + 1)
                if name_index < len(tokens) and tokens[name_index].kind in ("word", "ident"):
                    schemas.add(identifier_name(tokens[name_index]))
                return 0
            if kind == "EXTENSION":
                dialect_markers.add("extension")
                name_index = _skip_if_not_exists(tokens, index + 1)
                if name_index < len(tokens):
                    extensions.add(identifier_name(tokens[name_index]))
                return 0
            if kind == "TYPE":
                name, after = _qualified_name(tokens, index + 1)
                if after + 1 < len(tokens) and tokens[after].is_word("AS") and tokens[after + 1].is_word("ENUM"):
                    values, _ = paren_group(tokens, after + 2)
                    enum_types[name] = [token.text for token in values if token.kind == "string"]
                    return 0
                raise StatementError(f"CREATE TYPE {name} is not an enum type and is not modeled")
            if kind in ("VIEW", "FUNCTION", "PROCEDURE", "TRIGGER", "RULE", "POLICY", "DOMAIN", "AGGREGATE", "OPERATOR", "CAST", "COLLATION", "PUBLICATION", "SUBSCRIPTION", "SERVER", "FOREIGN"):
                label = "MATERIALIZED VIEW" if index > 0 and tokens[index - 1].is_word("MATERIALIZED") else kind
                if kind in ("FUNCTION", "PROCEDURE"):
                    dialect_markers.add("plpgsql")
                raise StatementError(f"CREATE {label} is not modeled (views, procedural code, triggers, and policies are reported, not analyzed)")
            if kind in ("ROLE", "USER", "DATABASE", "SEQUENCE", "TABLESPACE", "GROUP"):
                if kind == "TABLESPACE":
                    dialect_markers.add("tablespace")
                return 0
            raise StatementError(f"unsupported statement {statement.head}")
        if head == "ALTER" and len(tokens) > 1 and tokens[1].is_word("TABLE"):
            self._alter_table(context, parsed, tokens, span, tables, dialect_markers, result)
            return 0
        if head == "DROP" and len(tokens) > 1 and tokens[1].is_word("TABLE"):
            index = _skip_if_exists(tokens, 2)
            for item in split_items(tokens[index:]):
                item = [token for token in item if not token.is_word("CASCADE", "RESTRICT")]
                if not item:
                    continue
                name, _ = _qualified_name(item, 0)
                tables.pop(name, None)
                for table in tables.values():
                    table.foreign_keys = [fk for fk in table.foreign_keys if fk.references_table != name]
            return 0
        if head == "DO":
            raise StatementError("DO block contains procedural code that is not analyzed")
        if head in DATA_HEADS:
            return 1
        if any(words[: len(ignored)] == list(ignored) for ignored in IGNORED_HEADS):
            return 0
        if head == "ALTER" and len(tokens) > 1 and tokens[1].is_word("INDEX", "VIEW", "FUNCTION", "TRIGGER", "SCHEMA", "DATABASE", "EXTENSION", "DOMAIN", "MATERIALIZED", "POLICY", "DEFAULT"):
            if tokens[1].is_word("VIEW", "FUNCTION", "TRIGGER", "MATERIALIZED", "POLICY", "DOMAIN"):
                raise StatementError(f"ALTER {tokens[1].upper} is not modeled")
            return 0
        if head == "DROP" and len(tokens) > 1:
            return 0
        if head.startswith("\\"):
            raise StatementError("psql meta-command is not SQL")
        raise StatementError(f"unsupported statement {statement.head}")

    def _create_table(
        self,
        context: RepositoryContext,
        parsed: SqlFile,
        tokens: list[Token],
        index: int,
        span: dict[str, Any],
        tables: dict[str, Table],
        dialect_markers: set[str],
        result: AnalysisResult,
    ) -> None:
        index = _skip_if_not_exists(tokens, index)
        key, index = _qualified_name(tokens, index)
        schema, _, name = key.rpartition(".")
        table = Table(schema=schema or None, name=name)
        table.created_line = parsed.line(tokens[0].offset)
        table.created_path = parsed.path
        result.evidence.append(span)
        table.evidence_ids.append(span["id"])
        table.defined_in.append(parsed.path)
        if index < len(tokens) and tokens[index].is_word("PARTITION") and index + 1 < len(tokens) and tokens[index + 1].is_word("OF"):
            parent_key, _ = _qualified_name(tokens, index + 2)
            table.partition_of = parent_key
            parent = tables.get(parent_key)
            if parent is None:
                tables[key] = table
                result.limitations.append(
                    context.limitation(
                        LIMITATION_UNSUPPORTED_CONSTRUCT,
                        f"{parsed.path} line {table.created_line}: table {key} is a partition of {parent_key}, which is not defined in the scanned SQL; its columns are unknown.",
                        [span["id"]],
                    )
                )
                return
            table.columns = [Column(**vars(column)) for column in parent.columns]
            table.primary_key = list(parent.primary_key)
            tables[key] = table
            return
        if index < len(tokens) and tokens[index].is_word("AS"):
            raise StatementError(f"CREATE TABLE {key} AS query: columns come from a query and are not modeled")
        if index >= len(tokens) or not (tokens[index].kind == "punct" and tokens[index].text == "("):
            raise StatementError(f"CREATE TABLE {key} without a column list")
        items, after = paren_group(tokens, index)
        tables[key] = table
        deferred: list[str] = []
        for item in split_items(items):
            if not item:
                continue
            first = item[0].upper
            if first in TABLE_CONSTRAINT_WORDS and not (item[0].kind == "ident"):
                if first == "LIKE":
                    deferred.append(f"CREATE TABLE {key}: LIKE copies columns from another table and was not applied")
                    continue
                if first == "EXCLUDE" or (first == "CONSTRAINT" and len(item) > 2 and item[2].is_word("EXCLUDE")):
                    deferred.append(f"CREATE TABLE {key}: EXCLUDE constraints are not modeled")
                    continue
                self._table_constraint(parsed, item, table, span)
            else:
                table.columns.append(self._column(parsed, item, table, span, dialect_markers))
        rest = tokens[after:]
        for index_in_rest, token in enumerate(rest):
            if token.is_word("INHERITS"):
                dialect_markers.add("inherits")
                deferred.append(f"CREATE TABLE {key}: INHERITS adds columns from parent tables that were not applied")
            if token.is_word("PARTITION") and index_in_rest + 1 < len(rest) and rest[index_in_rest + 1].is_word("BY"):
                deferred.append(f"CREATE TABLE {key}: a partitioned parent stores its rows in partitions; only the declared columns are recorded")
        for message in deferred:
            result.limitations.append(context.limitation(LIMITATION_UNSUPPORTED_CONSTRUCT, message, [span["id"]]))

    def _column(
        self,
        parsed: SqlFile,
        item: list[Token],
        table: Table,
        span: dict[str, Any],
        dialect_markers: set[str],
    ) -> Column:
        if item[0].kind not in ("word", "ident"):
            raise StatementError(f"unexpected column definition starting with {item[0].text!r}")
        column = Column(name=identifier_name(item[0]), type="", line=parsed.line(item[0].offset), path=parsed.path)
        index = 1
        depth = 0
        type_end = index
        while type_end < len(item):
            token = item[type_end]
            if token.kind == "punct" and token.text == "(":
                depth += 1
            elif token.kind == "punct" and token.text == ")":
                depth -= 1
            elif depth == 0 and token.kind == "word" and token.upper in COLUMN_CONSTRAINT_WORDS:
                break
            type_end += 1
        if type_end == index:
            raise StatementError(f"column {column.name} has no data type")
        column.type = parsed.slice(item[index], item[type_end - 1])
        _record_type(column.type, dialect_markers)
        index = type_end
        while index < len(item):
            token = item[index]
            word = token.upper
            if word == "CONSTRAINT":
                index += 2
                continue
            if word == "NOT" and index + 1 < len(item) and item[index + 1].is_word("NULL"):
                column.nullable = False
                index += 2
                continue
            if word == "NULL":
                index += 1
                continue
            if word == "DEFAULT":
                expression_end = _expression_end(item, index + 1)
                column.default = parsed.slice(item[index + 1], item[expression_end - 1]) if expression_end > index + 1 else None
                index = expression_end
                continue
            if word == "PRIMARY":
                column.primary_key = True
                column.nullable = False
                table.primary_key = [column.name]
                index += 2
                continue
            if word == "UNIQUE":
                column.unique = True
                index += 1
                while index < len(item) and item[index].is_word("NULLS", "NOT", "DISTINCT"):
                    index += 1
                continue
            if word == "REFERENCES":
                fk, index = self._references(item, index + 1, [column.name], None, span)
                table.foreign_keys.append(fk)
                continue
            if word == "CHECK":
                _, index = paren_group(item, index + 1)
                table.checks.append(None)
                continue
            if word == "GENERATED":
                index += 1
                while index < len(item) and item[index].is_word("ALWAYS", "BY", "DEFAULT"):
                    index += 1
                if index < len(item) and item[index].is_word("AS"):
                    index += 1
                    if index < len(item) and item[index].is_word("IDENTITY"):
                        column.identity = True
                        column.nullable = False
                        index += 1
                        if index < len(item) and item[index].kind == "punct" and item[index].text == "(":
                            _, index = paren_group(item, index)
                        continue
                    if index < len(item) and item[index].kind == "punct" and item[index].text == "(":
                        _, index = paren_group(item, index)
                        column.generated = True
                        if index < len(item) and item[index].is_word("STORED"):
                            index += 1
                        continue
                raise StatementError(f"column {column.name}: unsupported GENERATED clause")
            if word == "COLLATE":
                index += 2
                continue
            if word in ("DEFERRABLE", "INITIALLY", "DEFERRED", "IMMEDIATE"):
                index += 1
                continue
            raise StatementError(f"column {column.name}: unsupported clause {token.text!r}")
        return column

    def _table_constraint(self, parsed: SqlFile, item: list[Token], table: Table, span: dict[str, Any]) -> None:
        index = 0
        name: str | None = None
        if item[index].is_word("CONSTRAINT"):
            name = identifier_name(item[index + 1])
            index += 2
        word = item[index].upper if index < len(item) else ""
        if word == "PRIMARY":
            columns, _ = paren_group(item, index + 2)
            table.primary_key = [identifier_name(token) for token in columns if token.kind in ("word", "ident")]
            table.primary_key_name = name
            for column in table.columns:
                if column.name in table.primary_key:
                    column.nullable = False
            return
        if word == "UNIQUE":
            index += 1
            while index < len(item) and item[index].is_word("NULLS", "NOT", "DISTINCT"):
                index += 1
            columns, _ = paren_group(item, index)
            table.unique_constraints.append(
                {"name": name, "columns": [identifier_name(token) for token in columns if token.kind in ("word", "ident")]}
            )
            return
        if word == "FOREIGN":
            columns, after = paren_group(item, index + 2)
            local = [identifier_name(token) for token in columns if token.kind in ("word", "ident")]
            if after >= len(item) or not item[after].is_word("REFERENCES"):
                raise StatementError("FOREIGN KEY without REFERENCES")
            fk, _ = self._references(item, after + 1, local, name, span)
            table.foreign_keys.append(fk)
            return
        if word == "CHECK":
            table.checks.append(name)
            return
        raise StatementError(f"unsupported table constraint {word}")

    def _references(
        self, item: list[Token], index: int, local: list[str], name: str | None, span: dict[str, Any]
    ) -> tuple[ForeignKey, int]:
        target, index = _qualified_name(item, index)
        columns: list[str] = []
        implicit = True
        if index < len(item) and item[index].kind == "punct" and item[index].text == "(":
            group, index = paren_group(item, index)
            columns = [identifier_name(token) for token in group if token.kind in ("word", "ident")]
            implicit = False
        on_delete = on_update = None
        while index < len(item):
            token = item[index]
            if token.is_word("MATCH"):
                index += 2
                continue
            if token.is_word("ON") and index + 1 < len(item):
                trigger = item[index + 1].upper
                if trigger not in ("DELETE", "UPDATE"):
                    break
                action, index = _fk_action(item, index + 2)
                if trigger == "DELETE":
                    on_delete = action
                else:
                    on_update = action
                continue
            if token.is_word("DEFERRABLE", "NOT", "INITIALLY", "DEFERRED", "IMMEDIATE"):
                index += 1
                continue
            break
        return ForeignKey(name, local, target, columns, on_delete, on_update, span["id"], implicit), index

    def _create_index(
        self, parsed: SqlFile, tokens: list[Token], index: int, span: dict[str, Any], tables: dict[str, Table], result: AnalysisResult
    ) -> None:
        unique = any(token.is_word("UNIQUE") for token in tokens[:index])
        if index < len(tokens) and tokens[index].is_word("CONCURRENTLY"):
            index += 1
        index = _skip_if_not_exists(tokens, index)
        name: str | None = None
        if index < len(tokens) and not tokens[index].is_word("ON"):
            name = identifier_name(tokens[index])
            index += 1
        if index >= len(tokens) or not tokens[index].is_word("ON"):
            raise StatementError("CREATE INDEX without ON")
        index += 1
        if tokens[index].is_word("ONLY"):
            index += 1
        table_key, index = _qualified_name(tokens, index)
        if index < len(tokens) and tokens[index].is_word("USING"):
            index += 2
        if index >= len(tokens) or not (tokens[index].kind == "punct" and tokens[index].text == "("):
            raise StatementError("CREATE INDEX without a column list")
        group, _ = paren_group(tokens, index)
        columns = [parsed.slice(part[0], part[-1]) for part in split_items(group) if part]
        table = tables.get(table_key)
        if table is None:
            raise StatementError(f"CREATE INDEX on {table_key}, which is not defined in the scanned SQL")
        result.evidence.append(span)
        table.evidence_ids.append(span["id"])
        table.defined_in.append(parsed.path)
        table.indexes.append({"name": name, "columns": columns, "unique": unique})

    def _alter_table(
        self,
        context: RepositoryContext,
        parsed: SqlFile,
        tokens: list[Token],
        span: dict[str, Any],
        tables: dict[str, Table],
        dialect_markers: set[str],
        result: AnalysisResult,
    ) -> None:
        index = _skip_if_exists(tokens, 2)
        if index < len(tokens) and tokens[index].is_word("ONLY"):
            index += 1
        key, index = _qualified_name(tokens, index)
        if index < len(tokens) and tokens[index].kind == "punct" and tokens[index].text == "*":
            index += 1
        table = tables.get(key)
        if table is None:
            result.evidence.append(span)
            result.limitations.append(
                context.limitation(
                    LIMITATION_AMBIGUOUS_REFERENCE,
                    f"{parsed.path} line {parsed.line(tokens[0].offset)}: ALTER TABLE {key} targets a table that is not defined in the scanned SQL; the change was not applied.",
                    [span["id"]],
                )
            )
            return
        result.evidence.append(span)
        table.evidence_ids.append(span["id"])
        table.defined_in.append(parsed.path)
        for action in split_items(tokens[index:]):
            if not action:
                continue
            word = action[0].upper
            if word == "ADD":
                i = 1
                if i < len(action) and action[i].is_word("COLUMN"):
                    i += 1
                i = _skip_if_not_exists(action, i)
                if i < len(action) and action[i].upper in TABLE_CONSTRAINT_WORDS and action[i].kind == "word":
                    if action[i].is_word("EXCLUDE") or (action[i].is_word("CONSTRAINT") and len(action) > i + 2 and action[i + 2].is_word("EXCLUDE")):
                        raise StatementError(f"ALTER TABLE {key}: EXCLUDE constraints are not modeled")
                    self._table_constraint(parsed, action[i:], table, span)
                    continue
                table.columns.append(self._column(parsed, action[i:], table, span, dialect_markers))
                continue
            if word == "DROP":
                i = 1
                if i < len(action) and action[i].is_word("CONSTRAINT"):
                    i = _skip_if_exists(action, i + 1)
                    name = identifier_name(action[i])
                    table.foreign_keys = [fk for fk in table.foreign_keys if fk.name != name]
                    table.unique_constraints = [uc for uc in table.unique_constraints if uc["name"] != name]
                    if table.primary_key_name == name:
                        table.primary_key = []
                        table.primary_key_name = None
                    continue
                if i < len(action) and action[i].is_word("COLUMN"):
                    i += 1
                i = _skip_if_exists(action, i)
                name = identifier_name(action[i])
                table.columns = [column for column in table.columns if column.name != name]
                table.primary_key = [column for column in table.primary_key if column != name]
                table.foreign_keys = [fk for fk in table.foreign_keys if name not in fk.columns]
                continue
            if word == "RENAME":
                i = 1
                if i < len(action) and action[i].is_word("TO"):
                    new_name = identifier_name(action[i + 1])
                    tables.pop(key)
                    new_key = f"{table.schema}.{new_name}" if table.schema else new_name
                    table.name = new_name
                    tables[new_key] = table
                    for other in tables.values():
                        for fk in other.foreign_keys:
                            if fk.references_table == key:
                                fk.references_table = new_key
                    continue
                if i < len(action) and action[i].is_word("COLUMN"):
                    i += 1
                if i < len(action) and action[i].is_word("CONSTRAINT"):
                    old = identifier_name(action[i + 1])
                    new = identifier_name(action[i + 3])
                    for fk in table.foreign_keys:
                        if fk.name == old:
                            fk.name = new
                    for uc in table.unique_constraints:
                        if uc["name"] == old:
                            uc["name"] = new
                    continue
                old = identifier_name(action[i])
                new = identifier_name(action[i + 2])
                column = table.column(old)
                if column is not None:
                    column.name = new
                table.primary_key = [new if name == old else name for name in table.primary_key]
                for fk in table.foreign_keys:
                    fk.columns = [new if name == old else name for name in fk.columns]
                continue
            if word == "ALTER":
                i = 1
                if i < len(action) and action[i].is_word("COLUMN"):
                    i += 1
                column = table.column(identifier_name(action[i]))
                if column is None:
                    raise StatementError(f"ALTER TABLE {key}: column {identifier_name(action[i])} is not defined")
                rest = action[i + 1 :]
                upper = [token.upper for token in rest]
                if upper[:2] == ["SET", "NOT"]:
                    column.nullable = False
                elif upper[:2] == ["DROP", "NOT"]:
                    column.nullable = True
                elif upper[:1] == ["TYPE"] or upper[:3] == ["SET", "DATA", "TYPE"]:
                    start = 1 if upper[:1] == ["TYPE"] else 3
                    end = next((k for k, w in enumerate(upper) if k >= start and w in ("USING", "COLLATE")), len(upper))
                    column.type = parsed.slice(rest[start], rest[end - 1])
                elif upper[:2] == ["SET", "DEFAULT"]:
                    column.default = parsed.slice(rest[2], rest[-1]) if len(rest) > 2 else None
                elif upper[:2] == ["DROP", "DEFAULT"]:
                    column.default = None
                elif upper[:2] == ["DROP", "IDENTITY"]:
                    column.identity = False
                elif upper[:2] == ["ADD", "GENERATED"]:
                    column.identity = True
                    column.nullable = False
                else:
                    raise StatementError(f"ALTER TABLE {key}: unsupported ALTER COLUMN action {' '.join(upper[:3])}")
                continue
            if word in ("ENABLE", "DISABLE", "OWNER", "SET", "RESET", "CLUSTER", "REPLICA", "INHERIT", "NO", "OF", "NOT", "ATTACH", "DETACH", "VALIDATE", "FORCE"):
                raise StatementError(f"ALTER TABLE {key}: action {' '.join(t.upper for t in action[:3])} is not modeled")
            raise StatementError(f"ALTER TABLE {key}: unsupported action {word}")


# ------------------------------------------------------------------ utilities
def migration_order(path: str) -> tuple[int, tuple[int, ...], str]:
    """Flyway versioned files by version, then repeatable files, then other files by path."""

    name = path.rsplit("/", 1)[-1]
    match = FLYWAY_VERSIONED.match(name)
    if match:
        parts = tuple(int(part) for part in re.split(r"[._]", match.group("version")) if part.isdigit())
        return (0, parts, path)
    if FLYWAY_REPEATABLE.match(name):
        return (1, (), path)
    return (2, (), path)


def _creates_table(statement: Statement) -> bool:
    """True when the statement defines or alters a table."""

    tokens = statement.tokens
    if not tokens:
        return False
    if tokens[0].is_word("CREATE"):
        index = 1
        while index < len(tokens) and tokens[index].is_word(*CREATE_MODIFIERS):
            index += 1
        return index < len(tokens) and tokens[index].is_word("TABLE")
    if tokens[0].is_word("ALTER"):
        return len(tokens) > 1 and tokens[1].is_word("TABLE")
    return False


def _record_type(declared: str, dialect_markers: set[str]) -> None:
    """Record PostgreSQL-only type names a column was declared with."""

    name = declared.strip().lower()
    array = name.endswith("[]")
    if array:
        name = name[:-2].strip()
    base = name.split("(")[0].strip()
    if base in POSTGRESQL_TYPES:
        dialect_markers.add(base)
    if array:
        dialect_markers.add("array")


def _skip_if_not_exists(tokens: list[Token], index: int) -> int:
    if index + 2 < len(tokens) and tokens[index].is_word("IF") and tokens[index + 1].is_word("NOT") and tokens[index + 2].is_word("EXISTS"):
        return index + 3
    return index


def _skip_if_exists(tokens: list[Token], index: int) -> int:
    if index + 1 < len(tokens) and tokens[index].is_word("IF") and tokens[index + 1].is_word("EXISTS"):
        return index + 2
    return index


def _qualified_name(tokens: list[Token], index: int) -> tuple[str, int]:
    """``schema.name`` or ``name`` (lower-cased unless quoted); returns the key and the next index."""

    parts: list[str] = []
    while index < len(tokens) and tokens[index].kind in ("word", "ident"):
        parts.append(identifier_name(tokens[index]))
        index += 1
        if index < len(tokens) and tokens[index].kind == "punct" and tokens[index].text == ".":
            index += 1
            continue
        break
    if not parts:
        raise StatementError("expected a table name")
    if len(parts) > 1 and parts[-2] == "public":
        parts = parts[-1:]
    return ".".join(parts[-2:]), index


def _expression_end(item: list[Token], index: int) -> int:
    depth = 0
    while index < len(item):
        token = item[index]
        if token.kind == "punct" and token.text == "(":
            depth += 1
        elif token.kind == "punct" and token.text == ")":
            depth -= 1
        elif depth == 0 and token.kind == "word" and token.upper in COLUMN_CONSTRAINT_WORDS and token.upper != "NULL":
            break
        index += 1
    return index


def _fk_action(item: list[Token], index: int) -> tuple[str, int]:
    if index >= len(item):
        raise StatementError("incomplete referential action")
    word = item[index].upper
    if word == "NO":
        return "NO ACTION", index + 2
    if word == "SET":
        return f"SET {item[index + 1].upper}", index + 2
    if word in ("CASCADE", "RESTRICT"):
        return word, index + 1
    raise StatementError(f"unsupported referential action {word}")
