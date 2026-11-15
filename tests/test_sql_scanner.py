"""SQL tokenizer: comments, literals, identifiers, and statement splitting."""

from __future__ import annotations

from repodocs.analyzers.sql import identifier_name, mask_comments, parse_sql


def statements_of(text: str) -> list[str]:
    parsed = parse_sql("schema.sql", text)
    return [" ".join(token.upper for token in statement.tokens) for statement in parsed.statements]


def test_block_comments_nest() -> None:
    text = "/* outer /* inner */ still outer */\nCREATE TABLE t (id INT);\n"
    masked = mask_comments(text)
    assert "outer" not in masked and "inner" not in masked
    assert statements_of(text) == ["CREATE TABLE T ( ID INT )"]


def test_unterminated_block_comment_masks_the_rest_of_the_file() -> None:
    text = "/* outer /* inner */\nCREATE TABLE hidden (id INT);\n"
    assert statements_of(text) == []


def test_comment_markers_inside_literals_are_kept() -> None:
    text = "INSERT INTO t VALUES ('/* not a comment */');\nCREATE TABLE u (id INT);\n"
    assert statements_of(text) == [
        "INSERT INTO T VALUES ( /* not a comment */ )",
        "CREATE TABLE U ( ID INT )",
    ]


def test_dollar_quoted_body_does_not_split_statements() -> None:
    text = (
        "CREATE FUNCTION f() RETURNS trigger AS $$\n"
        "BEGIN\n"
        "    NEW.updated_at = now(); -- a semicolon inside the body\n"
        "    RETURN NEW;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n"
        "CREATE TABLE v (id INT);\n"
    )
    assert len(statements_of(text)) == 2
    assert statements_of(text)[1] == "CREATE TABLE V ( ID INT )"


def test_statements_split_on_top_level_semicolons_only() -> None:
    text = "CREATE TABLE t (id NUMERIC(12, 2) NOT NULL);\nCREATE TABLE u (id INT);\n"
    assert len(statements_of(text)) == 2


def test_quoted_identifiers_keep_their_case_and_unquoted_ones_fold() -> None:
    parsed = parse_sql("schema.sql", 'CREATE TABLE "MixedCase" (plain INT);\n')
    tokens = parsed.statements[0].tokens
    assert identifier_name(tokens[2]) == "MixedCase"
    assert identifier_name(tokens[4]) == "plain"
