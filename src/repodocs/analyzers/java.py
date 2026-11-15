"""A small structural scanner for Java source files.

This is deliberately not a Java parser. It recovers the declarations that framework
adapters need (package, imports, types, annotations, fields, methods, constructor
parameters) with their line numbers, and it skips method bodies and initializers.
Comments are masked before scanning so that comment text can never produce facts.
Anything it cannot recover is left out rather than guessed.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field


MODIFIERS = {
    "public",
    "private",
    "protected",
    "static",
    "final",
    "abstract",
    "transient",
    "volatile",
    "synchronized",
    "native",
    "strictfp",
    "default",
    "sealed",
    "non-sealed",
}
TYPE_KEYWORDS = ("class", "interface", "enum", "record")
WORD = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
QUALIFIED = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.]*")


@dataclass(frozen=True)
class Annotation:
    name: str
    args: str
    line: int
    offset: int

    @property
    def simple_name(self) -> str:
        return self.name.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class Parameter:
    type: str
    name: str
    annotations: tuple[Annotation, ...]


@dataclass
class FieldDecl:
    name: str
    type: str
    line: int
    annotations: list[Annotation]
    static: bool

    def annotation(self, *names: str) -> Annotation | None:
        wanted = set(names)
        for item in self.annotations:
            if item.simple_name in wanted:
                return item
        return None


@dataclass
class MethodDecl:
    name: str
    return_type: str | None
    parameters: list[Parameter]
    line: int
    end_line: int
    annotations: list[Annotation]
    is_constructor: bool

    def annotation(self, *names: str) -> Annotation | None:
        wanted = set(names)
        for item in self.annotations:
            if item.simple_name in wanted:
                return item
        return None

    @property
    def first_line(self) -> int:
        return min([self.line, *[item.line for item in self.annotations]])


@dataclass
class TypeDecl:
    kind: str
    name: str
    line: int
    end_line: int
    annotations: list[Annotation]
    extends: list[str]
    implements: list[str]
    fields: list[FieldDecl] = field(default_factory=list)
    methods: list[MethodDecl] = field(default_factory=list)
    outer: str | None = None
    body_depth: int = 0

    def annotation(self, *names: str) -> Annotation | None:
        wanted = set(names)
        for item in self.annotations:
            if item.simple_name in wanted:
                return item
        return None

    def has_annotation(self, *names: str) -> bool:
        return self.annotation(*names) is not None

    @property
    def first_line(self) -> int:
        return min([self.line, *[item.line for item in self.annotations]])


@dataclass
class JavaUnit:
    path: str
    text: str
    package: str | None
    imports: list[str]
    types: list[TypeDecl]

    def qualified(self, type_name: str) -> str:
        return f"{self.package}.{type_name}" if self.package else type_name


def mask_comments(source: str) -> str:
    """Replace comment characters with spaces, preserving offsets and newlines."""

    out = []
    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        if c == '"':
            if source.startswith('"""', i):
                end = source.find('"""', i + 3)
                end = n if end == -1 else end + 3
            else:
                end = _skip_string(source, i, '"')
            out.append(source[i:end])
            i = end
        elif c == "'":
            end = _skip_string(source, i, "'")
            out.append(source[i:end])
            i = end
        elif c == "/" and source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif c == "/" and source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in source[i:end]))
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _skip_string(text: str, i: int, quote: str) -> int:
    i += 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == quote or c == "\n":
            return i + 1
        i += 1
    return n


def _skip_literal(text: str, i: int) -> int:
    if text.startswith('"""', i):
        end = text.find('"""', i + 3)
        return len(text) if end == -1 else end + 3
    return _skip_string(text, i, text[i])


def skip_balanced(text: str, i: int, open_char: str, close_char: str) -> int:
    """Return the index just after the bracket that closes ``text[i]`` (which must be ``open_char``)."""

    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            i = _skip_literal(text, i)
            continue
        if c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def line_index(text: str) -> list[int]:
    starts = [0]
    for index, character in enumerate(text):
        if character == "\n":
            starts.append(index + 1)
    return starts


def _line_at(starts: list[int], offset: int) -> int:
    return bisect.bisect_right(starts, offset)


def split_top_level(text: str, separator: str = ",") -> list[str]:
    """Split on a separator that is not nested in (), {}, [], <> or a string literal."""

    parts: list[str] = []
    depth = 0
    current: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            end = _skip_literal(text, i)
            current.append(text[i:end])
            i = end
            continue
        if c in "({[<":
            depth += 1
        elif c in ")}]>":
            depth -= 1
        if c == separator and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(c)
        i += 1
    if current or parts:
        parts.append("".join(current))
    return parts


def strip_generics(type_name: str) -> str:
    """``List<Order>`` becomes ``List``; ``Order[]`` becomes ``Order``; ``a.b.C`` becomes ``C``."""

    cleaned = type_name.strip()
    if "<" in cleaned:
        cleaned = cleaned[: cleaned.index("<")]
    cleaned = cleaned.replace("[]", "").replace("...", "").strip()
    return cleaned.rsplit(".", 1)[-1]


def generic_arguments(type_name: str) -> list[str]:
    start = type_name.find("<")
    if start == -1:
        return []
    end = skip_balanced(type_name, start, "<", ">")
    return [part.strip() for part in split_top_level(type_name[start + 1 : end - 1]) if part.strip()]


def _collapse_generics(head: str) -> str:
    out = []
    depth = 0
    for c in head:
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
        if c.isspace() and depth > 0:
            continue
        out.append(c)
    return "".join(out)


def _parse_annotations(part: str, i: int, starts: list[int], base_offset: int) -> tuple[list[Annotation], int]:
    annotations: list[Annotation] = []
    while i < len(part):
        if part[i].isspace():
            i += 1
            continue
        if part[i] != "@":
            break
        match = QUALIFIED.match(part, i + 1)
        if not match:
            break
        j = match.end()
        while j < len(part) and part[j].isspace():
            j += 1
        args = ""
        if j < len(part) and part[j] == "(":
            end = skip_balanced(part, j, "(", ")")
            args = part[j + 1 : end - 1]
            j = end
        annotations.append(Annotation(match.group(0), args, _line_at(starts, base_offset + i), base_offset + i))
        i = j
    return annotations, i


def _parse_parameters(text: str, starts: list[int], base_offset: int) -> list[Parameter]:
    parameters: list[Parameter] = []
    offset = 0
    for raw in split_top_level(text):
        part_offset = base_offset + offset
        offset += len(raw) + 1
        leading = len(raw) - len(raw.lstrip())
        part = raw.strip()
        if not part:
            continue
        annotations, i = _parse_annotations(part, 0, starts, part_offset + leading)
        tokens = [token for token in _collapse_generics(part[i:]).split() if token not in MODIFIERS]
        if len(tokens) < 2:
            continue
        parameters.append(Parameter(type=tokens[-2], name=tokens[-1], annotations=tuple(annotations)))
    return parameters


def parse_java(path: str, source: str) -> JavaUnit:
    text = mask_comments(source)
    starts = line_index(text)
    n = len(text)
    package: str | None = None
    imports: list[str] = []
    types: list[TypeDecl] = []
    stack: list[TypeDecl] = []
    pending: list[Annotation] = []
    awaiting_body: TypeDecl | None = None
    depth = 0
    i = 0

    def current() -> TypeDecl | None:
        return stack[-1] if stack else None

    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c in "\"'":
            i = _skip_literal(text, i)
            continue
        if c == "@":
            match = QUALIFIED.match(text, i + 1)
            if not match:
                i += 1
                continue
            if match.group(0) == "interface":
                # Annotation type declaration: record it and skip its body.
                name_match = WORD.search(text, match.end())
                brace = text.find("{", name_match.end()) if name_match else -1
                if not name_match or brace == -1:
                    break
                decl = TypeDecl(
                    "annotation", name_match.group(0), _line_at(starts, name_match.start()), 0, list(pending), [], []
                )
                pending = []
                end = skip_balanced(text, brace, "{", "}")
                decl.end_line = _line_at(starts, end - 1)
                decl.outer = current().name if current() else None
                types.append(decl)
                i = end
                continue
            j = match.end()
            while j < n and text[j].isspace():
                j += 1
            args = ""
            if j < n and text[j] == "(":
                end = skip_balanced(text, j, "(", ")")
                args = text[j + 1 : end - 1]
                j = end
            pending.append(Annotation(match.group(0), args, _line_at(starts, i), i))
            i = j
            continue
        if c == "{":
            if awaiting_body is not None:
                depth += 1
                awaiting_body.body_depth = depth
                stack.append(awaiting_body)
                awaiting_body = None
                pending = []
                if stack[-1].kind == "enum":
                    # Enum constants are not fields: skip to the first top-level ';' or the end.
                    end = skip_balanced(text, i, "{", "}")
                    body = text[i + 1 : end - 1]
                    semicolon = _find_top_level(body, ";")
                    if semicolon == -1:
                        stack[-1].end_line = _line_at(starts, end - 1)
                        stack.pop()
                        depth -= 1
                        i = end
                    else:
                        i = i + 1 + semicolon + 1
                    continue
                i += 1
                continue
            # A block that is not a type body (static initializer, unexpected brace): skip it.
            i = skip_balanced(text, i, "{", "}")
            pending = []
            continue
        if c == "}":
            if stack and depth == stack[-1].body_depth:
                stack[-1].end_line = _line_at(starts, i)
                stack.pop()
                depth -= 1
            pending = []
            i += 1
            continue
        if c == ";":
            pending = []
            i += 1
            continue
        word = WORD.match(text, i)
        if not word:
            i += 1
            continue
        token = word.group(0)
        if token == "package" and depth == 0:
            end = text.find(";", word.end())
            package = text[word.end() : end].strip() if end != -1 else None
            i = end + 1 if end != -1 else n
            continue
        if token == "import" and depth == 0:
            end = text.find(";", word.end())
            imports.append(" ".join(text[word.end() : end].split()) if end != -1 else "")
            i = end + 1 if end != -1 else n
            continue
        if token in TYPE_KEYWORDS and word.end() < n and text[word.end()].isspace():
            name_match = WORD.match(text, _skip_space(text, word.end()))
            if not name_match:
                i = word.end()
                continue
            j = _skip_space(text, name_match.end())
            if j < n and text[j] == "<":
                j = skip_balanced(text, j, "<", ">")
            record_components = ""
            components_offset = 0
            if token == "record":
                j = _skip_space(text, j)
                if j < n and text[j] == "(":
                    end = skip_balanced(text, j, "(", ")")
                    record_components = text[j + 1 : end - 1]
                    components_offset = j + 1
                    j = end
            brace = text.find("{", j)
            semicolon = text.find(";", j)
            if brace == -1 or (semicolon != -1 and semicolon < brace):
                i = j
                continue
            header = text[j:brace]
            decl = TypeDecl(
                kind=token,
                name=name_match.group(0),
                line=_line_at(starts, name_match.start()),
                end_line=0,
                annotations=list(pending),
                extends=_header_types(header, "extends"),
                implements=_header_types(header, "implements"),
                outer=current().name if current() else None,
            )
            if record_components:
                for parameter in _parse_parameters(record_components, starts, components_offset):
                    decl.fields.append(
                        FieldDecl(parameter.name, parameter.type, decl.line, list(parameter.annotations), False)
                    )
            pending = []
            types.append(decl)
            awaiting_body = decl
            i = brace
            continue
        owner = current()
        if owner is None or depth != owner.body_depth:
            i = word.end()
            continue
        # Member declaration head: everything up to '(' (method), '=' or ';' (field), or '{'.
        head_end, terminator = _find_head_end(text, i)
        head = _collapse_generics(text[i:head_end])
        all_tokens = head.split()
        static = "static" in all_tokens
        tokens = [item for item in all_tokens if item not in MODIFIERS]
        if tokens and tokens[0] in TYPE_KEYWORDS:
            # A nested type with modifiers: resume at the keyword so the type branch handles it.
            keyword = re.compile(rf"\b{tokens[0]}\b").search(text, i, head_end)
            if keyword is not None:
                i = keyword.start()
                continue
        if terminator == "(":
            params_end = skip_balanced(text, head_end, "(", ")")
            parameters = _parse_parameters(text[head_end + 1 : params_end - 1], starts, head_end + 1)
            body_end = _find_method_end(text, params_end)
            if tokens:
                name = tokens[-1]
                is_constructor = name == owner.name and len(tokens) == 1
                return_type = None if is_constructor else (tokens[-2] if len(tokens) >= 2 else None)
                if is_constructor or return_type is not None:
                    owner.methods.append(
                        MethodDecl(
                            name=name,
                            return_type=return_type,
                            parameters=parameters,
                            line=_line_at(starts, i),
                            end_line=_line_at(starts, max(body_end - 1, params_end)),
                            annotations=list(pending),
                            is_constructor=is_constructor,
                        )
                    )
            pending = []
            i = body_end
            continue
        if terminator in "=;":
            if len(tokens) >= 2:
                owner.fields.append(FieldDecl(tokens[-1], tokens[-2], _line_at(starts, i), list(pending), static))
            pending = []
            i = _find_statement_end(text, head_end)
            continue
        if terminator == "{":
            # A member block we do not model (for example an instance initializer): skip it.
            pending = []
            i = skip_balanced(text, head_end, "{", "}")
            continue
        pending = []
        i = max(head_end, word.end())
    return JavaUnit(path=path, text=text, package=package, imports=imports, types=types)


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _header_types(header: str, keyword: str) -> list[str]:
    match = re.search(rf"\b{keyword}\b", header)
    if not match:
        return []
    rest = header[match.end() :]
    for stop in ("extends", "implements", "permits"):
        stop_match = re.search(rf"\b{stop}\b", rest)
        if stop_match:
            rest = rest[: stop_match.start()]
    return [part.strip() for part in split_top_level(_collapse_generics(rest)) if part.strip()]


def _find_top_level(text: str, needle: str) -> int:
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c in "\"'":
            i = _skip_literal(text, i)
            continue
        if c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
        elif c == needle and depth == 0:
            return i
        i += 1
    return -1


def _find_head_end(text: str, i: int) -> tuple[int, str]:
    angle = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "<":
            angle += 1
        elif c == ">":
            angle = max(angle - 1, 0)
        elif angle == 0 and c in "(=;{":
            return i, c
        elif c == "}":
            return i, "}"
        i += 1
    return n, ""


def _find_method_end(text: str, i: int) -> int:
    """After a parameter list: skip ``throws`` clauses, then a body or a ';'."""

    n = len(text)
    while i < n:
        c = text[i]
        if c == "{":
            return skip_balanced(text, i, "{", "}")
        if c == ";":
            return i + 1
        if c == "}":
            return i
        if c == "(":
            i = skip_balanced(text, i, "(", ")")
            continue
        i += 1
    return n


def _find_statement_end(text: str, i: int) -> int:
    n = len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            i = _skip_literal(text, i)
            continue
        if c in "({[":
            close = {"(": ")", "{": "}", "[": "]"}[c]
            i = skip_balanced(text, i, c, close)
            continue
        if c == ";":
            return i + 1
        if c == "}":
            return i
        i += 1
    return n


@dataclass(frozen=True)
class AnnotationValue:
    """One annotation attribute: the string literals it holds, or the raw text when not literal."""

    literals: tuple[str, ...]
    raw: str
    is_literal: bool


_STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')


def annotation_values(args: str) -> dict[str, AnnotationValue]:
    """Parse ``name = "x", path = {"/a", "/b"}``; a bare value is keyed ``value``."""

    values: dict[str, AnnotationValue] = {}
    for part in split_top_level(args):
        part = part.strip()
        if not part:
            continue
        key = "value"
        body = part
        equals = _find_top_level(part, "=")
        if equals != -1 and not part[:equals].strip().startswith(("\"", "'")):
            key = part[:equals].strip()
            body = part[equals + 1 :].strip()
        inner = body[1:-1] if body.startswith("{") and body.endswith("}") else body
        elements = [item.strip() for item in split_top_level(inner) if item.strip()] if inner.strip() else []
        literals: list[str] = []
        is_literal = bool(elements)
        for element in elements:
            match = _STRING_LITERAL.fullmatch(element)
            if match:
                literals.append(bytes(match.group(1), "utf-8").decode("unicode_escape"))
            else:
                is_literal = False
        values[key] = AnnotationValue(tuple(literals), body, is_literal)
    return values
