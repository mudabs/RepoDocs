"""A small SQL tokenizer and statement splitter for static schema files.

Comments are masked, string literals (including dollar-quoted bodies), quoted
identifiers, numbers, words, and punctuation become tokens with offsets, and
statements are split on top-level semicolons. This is not a SQL parser; the
PostgreSQL adapter walks the tokens of the statement forms it supports and
reports every other statement as a limitation.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass


WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
NUMBER = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


@dataclass(frozen=True)
class Token:
    kind: str  # word | ident | string | number | punct
    text: str  # raw text as written (quotes stripped for ident and string)
    offset: int
    end: int

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind == "word" else self.text

    def is_word(self, *words: str) -> bool:
        return self.kind == "word" and self.text.upper() in words


@dataclass
class Statement:
    tokens: list[Token]
    start: int
    end: int

    @property
    def head(self) -> str:
        return " ".join(token.upper for token in self.tokens[:3])


@dataclass
class SqlFile:
    path: str
    text: str
    masked: str
    statements: list[Statement]
    line_starts: list[int]

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.line_starts, offset)

    def slice(self, first: Token, last: Token) -> str:
        """Original text between two tokens, whitespace-normalized."""

        return " ".join(self.text[first.offset : last.end].split())


def mask_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "-" and text.startswith("--", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif c == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:end]))
            i = end
        elif c == "'":
            end = _skip_single_quoted(text, i)
            out.append(text[i:end])
            i = end
        elif c == '"':
            end = _skip_double_quoted(text, i)
            out.append(text[i:end])
            i = end
        elif c == "$":
            match = DOLLAR_TAG.match(text, i)
            if match:
                close = text.find(match.group(0), match.end())
                end = n if close == -1 else close + len(match.group(0))
                out.append(text[i:end])
                i = end
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _skip_single_quoted(text: str, i: int) -> int:
    j = i + 1
    n = len(text)
    escaped = i > 0 and text[i - 1] in "eE"
    while j < n:
        c = text[j]
        if escaped and c == "\\":
            j += 2
            continue
        if c == "'":
            if j + 1 < n and text[j + 1] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _skip_double_quoted(text: str, i: int) -> int:
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == '"':
            if j + 1 < n and text[j + 1] == '"':
                j += 2
                continue
            return j + 1
        j += 1
    return n


def tokenize(masked: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    n = len(masked)
    while i < n:
        c = masked[i]
        if c.isspace():
            i += 1
            continue
        if c == "'":
            end = _skip_single_quoted(masked, i)
            tokens.append(Token("string", masked[i + 1 : end - 1].replace("''", "'"), i, end))
            i = end
            continue
        if c == '"':
            end = _skip_double_quoted(masked, i)
            tokens.append(Token("ident", masked[i + 1 : end - 1].replace('""', '"'), i, end))
            i = end
            continue
        if c == "$":
            match = DOLLAR_TAG.match(masked, i)
            if match:
                close = masked.find(match.group(0), match.end())
                end = n if close == -1 else close + len(match.group(0))
                tokens.append(Token("string", masked[match.end() : close if close != -1 else n], i, end))
                i = end
                continue
        word = WORD.match(masked, i)
        if word:
            tokens.append(Token("word", word.group(0), i, word.end()))
            i = word.end()
            continue
        number = NUMBER.match(masked, i)
        if number:
            tokens.append(Token("number", number.group(0), i, number.end()))
            i = number.end()
            continue
        if masked.startswith("::", i):
            tokens.append(Token("punct", "::", i, i + 2))
            i += 2
            continue
        tokens.append(Token("punct", c, i, i + 1))
        i += 1
    return tokens


def split_statements(tokens: list[Token]) -> list[Statement]:
    statements: list[Statement] = []
    current: list[Token] = []
    depth = 0
    for token in tokens:
        if token.kind == "punct" and token.text == "(":
            depth += 1
        elif token.kind == "punct" and token.text == ")":
            depth = max(depth - 1, 0)
        if token.kind == "punct" and token.text == ";" and depth == 0:
            if current:
                statements.append(Statement(current, current[0].offset, current[-1].end))
            current = []
            continue
        current.append(token)
    if current:
        statements.append(Statement(current, current[0].offset, current[-1].end))
    return statements


def parse_sql(path: str, text: str) -> SqlFile:
    masked = mask_comments(text)
    starts = [0]
    for index, character in enumerate(text):
        if character == "\n":
            starts.append(index + 1)
    return SqlFile(path=path, text=text, masked=masked, statements=split_statements(tokenize(masked)), line_starts=starts)


def split_items(tokens: list[Token]) -> list[list[Token]]:
    """Split a token list on commas at parenthesis depth 0."""

    items: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for token in tokens:
        if token.kind == "punct" and token.text == "(":
            depth += 1
        elif token.kind == "punct" and token.text == ")":
            depth -= 1
        if token.kind == "punct" and token.text == "," and depth == 0:
            items.append(current)
            current = []
            continue
        current.append(token)
    if current:
        items.append(current)
    return items


def paren_group(tokens: list[Token], start: int) -> tuple[list[Token], int]:
    """Tokens inside the parenthesis opening at ``start`` and the index after its close."""

    if start >= len(tokens) or not (tokens[start].kind == "punct" and tokens[start].text == "("):
        raise ValueError("expected (")
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token.kind == "punct" and token.text == "(":
            depth += 1
        elif token.kind == "punct" and token.text == ")":
            depth -= 1
            if depth == 0:
                return tokens[start + 1 : index], index + 1
    raise ValueError("unbalanced (")


def identifier_name(token: Token) -> str:
    """PostgreSQL folds unquoted identifiers to lower case; quoted identifiers keep their case."""

    return token.text if token.kind == "ident" else token.text.lower()
