"""A small structural scanner for JavaScript and TypeScript sources, including JSX.

Like the Java scanner this is not a parser. It masks comments and string
contents while remembering every string literal with its offset, so adapters can
match syntax on the masked text and read literal values from the tokens. It
recovers top-level statements, import and export declarations, and balanced
bracket ranges with line numbers. JSX text may contain apostrophes and quotes, so
a quote starts a string only in an operator position; what the scanner cannot
classify is left as code rather than guessed.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field


KEYWORDS_BEFORE_STRING = {
    "return",
    "case",
    "typeof",
    "in",
    "of",
    "from",
    "import",
    "export",
    "default",
    "delete",
    "void",
    "throw",
    "new",
    "yield",
    "await",
    "else",
    "do",
    "instanceof",
}
STRING_START_PUNCTUATION = set("(,=[:?+{;!&|<>~^%*-/")
STATEMENT_KEYWORDS = (
    "import",
    "export",
    "const",
    "let",
    "var",
    "function",
    "class",
    "async",
    "type",
    "interface",
    "enum",
    "declare",
    "if",
    "for",
    "while",
    "return",
    "switch",
    "try",
)
IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")


@dataclass(frozen=True)
class StringToken:
    offset: int
    end: int
    value: str
    kind: str
    expressions: tuple[str, ...] = ()

    @property
    def pattern(self) -> str:
        """Template text with each ``${expression}`` written as ``{expression}``."""

        if self.kind != "template":
            return self.value
        out = []
        i = 0
        text = self.value
        while i < len(text):
            if text.startswith("${", i):
                end = _match_template_expression(text, i)
                out.append("{" + " ".join(text[i + 2 : end - 1].split()) + "}")
                i = end
            else:
                out.append(text[i])
                i += 1
        return "".join(out)


@dataclass
class ImportDecl:
    specifier: str
    offset: int
    line: int
    kind: str  # static | dynamic | require | reexport
    default_name: str | None = None
    namespace_name: str | None = None
    named: dict[str, str] = field(default_factory=dict)  # local name -> imported name


@dataclass
class JsSource:
    path: str
    text: str
    masked: str
    strings: list[StringToken]
    line_starts: list[int]
    _by_offset: dict[int, StringToken] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_offset = {token.offset: token for token in self.strings}

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.line_starts, offset)

    def string_at(self, offset: int) -> StringToken | None:
        """The string token whose opening quote is at ``offset`` (leading whitespace skipped)."""

        while offset < len(self.masked) and self.masked[offset].isspace():
            offset += 1
        return self._by_offset.get(offset)


def scan_source(path: str, text: str) -> JsSource:
    masked, strings = mask_source(text)
    starts = [0]
    for index, character in enumerate(text):
        if character == "\n":
            starts.append(index + 1)
    return JsSource(path=path, text=text, masked=masked, strings=strings, line_starts=starts)


def mask_source(text: str) -> tuple[str, list[StringToken]]:
    out: list[str] = []
    strings: list[StringToken] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "/" and text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif c == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(_blank(text[i:end]))
            i = end
        elif c in "\"'" and _is_string_start(text, i):
            end = _skip_quoted(text, i, c)
            if end is None:
                out.append(c)
                i += 1
                continue
            strings.append(StringToken(i, end, _unescape(text[i + 1 : end - 1]), "string"))
            out.append(c + " " * (end - i - 2) + c)
            i = end
        elif c == "`":
            end, expressions = _scan_template(text, i)
            strings.append(StringToken(i, end, text[i + 1 : end - 1], "template", tuple(expressions)))
            out.append("`" + _blank(text[i + 1 : end - 1]) + "`")
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out), strings


def _blank(segment: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in segment)


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda match: {"n": "\n", "t": "\t"}.get(match.group(1), match.group(1)), value)


def _is_string_start(text: str, i: int) -> bool:
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j < 0 or text[j] == "\n":
        return True
    previous = text[j]
    if previous in STRING_START_PUNCTUATION:
        return True
    if previous.isalnum() or previous in "_$":
        start = j
        while start >= 0 and (text[start].isalnum() or text[start] in "_$"):
            start -= 1
        return text[start + 1 : j + 1] in KEYWORDS_BEFORE_STRING
    return False


def _skip_quoted(text: str, i: int, quote: str) -> int | None:
    """Index after the closing quote, or ``None`` when the line ends first (JSX text)."""

    j = i + 1
    n = len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == quote:
            return j + 1
        if c == "\n":
            return None
        j += 1
    return None


def _scan_template(text: str, i: int) -> tuple[int, list[str]]:
    expressions: list[str] = []
    j = i + 1
    n = len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            return j + 1, expressions
        if text.startswith("${", j):
            end = _match_template_expression(text, j)
            expressions.append(text[j + 2 : end - 1].strip())
            j = end
            continue
        j += 1
    return n, expressions


def _match_template_expression(text: str, i: int) -> int:
    """Index after the ``}`` that closes the ``${`` at ``i``."""

    depth = 0
    j = i + 1
    n = len(text)
    while j < n:
        c = text[j]
        if c in "\"'":
            end = _skip_quoted(text, j, c)
            j = end if end is not None else j + 1
            continue
        if c == "`":
            j, _ = _scan_template(text, j)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def skip_balanced(masked: str, i: int, open_char: str, close_char: str) -> int:
    """Index after the bracket closing ``masked[i]``; strings are already masked."""

    depth = 0
    n = len(masked)
    while i < n:
        c = masked[i]
        if c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def split_top_level(masked: str, start: int, end: int, separator: str = ",") -> list[tuple[int, int]]:
    """Offsets of the separator-delimited items of ``masked[start:end]`` at bracket depth 0."""

    items: list[tuple[int, int]] = []
    depth = 0
    item_start = start
    for i in range(start, end):
        c = masked[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == separator and depth == 0:
            items.append((item_start, i))
            item_start = i + 1
    if masked[item_start:end].strip():
        items.append((item_start, end))
    return items


def object_fields(masked: str, start: int, end: int) -> dict[str, tuple[int, int]]:
    """Top-level ``key: value`` fields of the object literal ``masked[start:end]`` (braces included)."""

    fields: dict[str, tuple[int, int]] = {}
    for item_start, item_end in split_top_level(masked, start + 1, end - 1):
        item = masked[item_start:item_end]
        match = re.match(r"\s*([A-Za-z_$][\w$]*)\s*:", item)
        if not match:
            continue
        fields[match.group(1)] = (item_start + match.end(), item_end)
    return fields


def top_level_statements(masked: str) -> list[tuple[int, int]]:
    """Start and end offsets of top-level statements, using ';', balanced blocks, and ASI heuristics."""

    statements: list[tuple[int, int]] = []
    n = len(masked)
    i = 0
    while i < n:
        while i < n and masked[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        depth = 0
        head = masked[start : start + 12]
        block_statement = bool(re.match(r"(export\s+(default\s+)?)?(async\s+)?(function|class)\b", masked[start : start + 40]))
        while i < n:
            c = masked[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth == 0 and c == "}":
                    j = i + 1
                    while j < n and masked[j] in " \t":
                        j += 1
                    if j < n and masked[j] == ";":
                        i = j + 1
                        break
                    if block_statement or j >= n or masked[j] == "\n":
                        i = i + 1
                        break
            elif c == ";" and depth == 0:
                i += 1
                break
            elif c == "\n" and depth == 0:
                j = i + 1
                while j < n and masked[j] in " \t\n":
                    j += 1
                if j >= n or re.match(r"(?:%s)\b" % "|".join(STATEMENT_KEYWORDS), masked[j:j + 12]) or masked[j] == "@":
                    i += 1
                    break
            i += 1
        statements.append((start, min(i, n)))
        del head
    return statements


_IMPORT_FROM = re.compile(r"\bimport\s+(?:type\s+)?(?P<clause>[^;]*?)\s+from\s*(?=[\"'])")
_IMPORT_SIDE_EFFECT = re.compile(r"\bimport\s*(?=[\"'])")
_IMPORT_DYNAMIC = re.compile(r"(?<![\w.$])import\s*\(")
_REQUIRE = re.compile(r"(?<![\w.$])require\s*\(\s*(?=[\"'])")
_REEXPORT = re.compile(r"\bexport\s+(?:type\s+)?(?:\*(?:\s+as\s+[\w$]+)?|\{[^}]*\})\s+from\s*(?=[\"'])")


def parse_imports(source: JsSource) -> list[ImportDecl]:
    """Static imports, re-exports, ``require`` calls, and ``import()`` calls with their specifiers."""

    masked = source.masked
    imports: list[ImportDecl] = []
    for match in _IMPORT_FROM.finditer(masked):
        token = source.string_at(match.end())
        if token is None:
            continue
        decl = ImportDecl(token.value, match.start(), source.line(match.start()), "static")
        _parse_import_clause(match.group("clause"), decl)
        imports.append(decl)
    for match in _IMPORT_SIDE_EFFECT.finditer(masked):
        token = source.string_at(match.end())
        if token is None or masked[match.start() : match.start() + 7] != "import " and masked[match.end() - 1] not in "\"'":
            continue
        # Skip "import x from '...'" matches that the side-effect pattern also sees inside clauses.
        if any(item.offset == match.start() for item in imports):
            continue
        imports.append(ImportDecl(token.value, match.start(), source.line(match.start()), "static"))
    for match in _REEXPORT.finditer(masked):
        token = source.string_at(match.end())
        if token is not None:
            imports.append(ImportDecl(token.value, match.start(), source.line(match.start()), "reexport"))
    for match in _REQUIRE.finditer(masked):
        token = source.string_at(match.end())
        if token is not None:
            imports.append(ImportDecl(token.value, match.start(), source.line(match.start()), "require"))
    for match in _IMPORT_DYNAMIC.finditer(masked):
        token = source.string_at(match.end())
        specifier = token.value if token is not None and token.kind == "string" else ""
        imports.append(ImportDecl(specifier, match.start(), source.line(match.start()), "dynamic"))
    imports.sort(key=lambda item: item.offset)
    return imports


def _parse_import_clause(clause: str, decl: ImportDecl) -> None:
    clause = clause.strip()
    if not clause:
        return
    brace = clause.find("{")
    head = clause if brace == -1 else clause[:brace]
    for part in head.split(","):
        part = part.strip()
        if not part:
            continue
        star = re.match(r"\*\s+as\s+([\w$]+)", part)
        if star:
            decl.namespace_name = star.group(1)
        elif IDENTIFIER.fullmatch(part):
            decl.default_name = part
    if brace != -1:
        inner = clause[brace + 1 : clause.rfind("}")]
        for part in inner.split(","):
            part = part.strip()
            if not part:
                continue
            part = re.sub(r"^type\s+", "", part)
            alias = re.match(r"([\w$]+)\s+as\s+([\w$]+)", part)
            if alias:
                decl.named[alias.group(2)] = alias.group(1)
            elif IDENTIFIER.fullmatch(part):
                decl.named[part] = part


_EXPORT_NAMED = re.compile(r"\bexport\s+(?:async\s+)?(?:function\s*\*?|class|const|let|var|enum|type|interface)\s+([A-Za-z_$][\w$]*)")
_EXPORT_DEFAULT_NAME = re.compile(r"\bexport\s+default\s+(?:async\s+)?(?:function\s*\*?\s*|class\s+)?([A-Z][\w$]*)\b")
_EXPORT_LIST = re.compile(r"\bexport\s*\{([^}]*)\}(?!\s*from)")


def parse_exports(masked: str) -> tuple[set[str], str | None]:
    """Named exports and the default-exported identifier when it is a plain identifier."""

    named: set[str] = set()
    for match in _EXPORT_NAMED.finditer(masked):
        named.add(match.group(1))
    for match in _EXPORT_LIST.finditer(masked):
        for part in match.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            alias = re.match(r"([\w$]+)\s+as\s+([\w$]+)", part)
            if alias:
                if alias.group(2) == "default":
                    named.add(alias.group(1))
                else:
                    named.add(alias.group(2))
            elif IDENTIFIER.fullmatch(part):
                named.add(part)
    default = None
    match = _EXPORT_DEFAULT_NAME.search(masked)
    if match:
        default = match.group(1)
    return named, default
