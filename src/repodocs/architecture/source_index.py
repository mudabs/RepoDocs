"""Static helpers for choosing and indexing source context (ADR-029).

Everything here reads text that the context resolver has already read and bounded. Nothing opens a
file, runs code, or follows a link. A reference is a statement a file makes about another file; it
is never evidence of runtime order, calls, or data flow.
"""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

LAYERS = ("backend", "frontend", "storage", "other")
MAX_REFERENCES_PER_FILE = 30
MAX_SPECIFIER_LENGTH = 120

CODE_SUFFIXES = {
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".py", ".java", ".kt", ".go", ".rs", ".cs", ".rb",
    ".php", ".html", ".htm", ".sql",
}
CONFIG_CANDIDATE_SUFFIXES = {".yml", ".yaml", ".properties", ".toml"}
ASSET_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg", ".avif", ".tif", ".tiff", ".woff", ".woff2",
    ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".webm", ".mov", ".wav", ".ogg", ".pdf", ".zip", ".gz", ".glb", ".gltf",
    ".obj", ".stl", ".psd", ".pptx", ".docx", ".xlsx",
}
VENDORED_DIRECTORIES = {
    "node_modules", "vendor", "dist", "build", "target", "out", "coverage", "__pycache__", ".venv", "venv",
    "bower_components", "third_party", ".next",
}
NOISE_TOKENS = {"test", "tests", "spec", "specs", "__tests__", "fixture", "fixtures", "mock", "mocks", "example", "examples", "docs", "doc"}
ROLE_TOKENS = {
    "route", "routes", "router", "controller", "controllers", "handler", "handlers", "api", "service", "services", "server",
    "model", "models", "schema", "schemas", "repository", "repositories", "dao", "migration", "migrations", "store", "db",
    "component", "components", "page", "pages", "view", "views", "app", "middleware",
}
STORAGE_TOKENS = {
    "db", "database", "model", "models", "migration", "migrations", "schema", "schemas", "repository", "repositories", "dao",
    "store", "stores", "storage", "entity", "entities", "orm", "prisma",
}
FRONTEND_TOKENS = {"public", "client", "frontend", "web", "static", "components", "pages", "views", "ui", "templates", "www"}
BACKEND_TOKENS = {"server", "api", "routes", "route", "controllers", "controller", "handlers", "handler", "services", "service", "backend", "middleware"}
FRONTEND_SUFFIXES = {".jsx", ".tsx", ".vue", ".svelte", ".html", ".htm", ".css", ".scss"}
BACKEND_SUFFIXES = {".py", ".java", ".kt", ".go", ".rs", ".cs", ".rb", ".php"}
_TOKEN = re.compile(r"[^a-z0-9]+")


def path_tokens(path: str) -> set[str]:
    return {token for token in _TOKEN.split(path.lower()) if token}


def is_asset(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in ASSET_SUFFIXES


def is_vendored(path: str) -> bool:
    return any(part.lower() in VENDORED_DIRECTORIES for part in path.split("/")[:-1])


def is_noise(path: str) -> bool:
    return bool(path_tokens("/".join(path.split("/")[:-1])) & NOISE_TOKENS) or path.rsplit("/", 1)[-1].lower().startswith(("test_", "mock"))


def layer_of(path: str) -> str:
    """The part of a system a path belongs to, from its name and location only (no content, so no claim)."""

    pure = PurePosixPath(path.lower())
    tokens = path_tokens(path)
    stem = pure.stem
    if pure.suffix == ".sql" or tokens & STORAGE_TOKENS or stem.endswith(tuple(STORAGE_TOKENS - {"db"})):
        return "storage"
    if pure.suffix in FRONTEND_SUFFIXES or tokens & FRONTEND_TOKENS:
        return "frontend"
    if pure.suffix in BACKEND_SUFFIXES or tokens & BACKEND_TOKENS:
        return "backend"
    return "other"


# ------------------------------------------------------------------ spans
_SIGNAL = re.compile(
    r"^\s*(?:export\s+|async\s+|public\s+|private\s+|protected\s+|static\s+|default\s+)*(?:function|class|def|interface|enum|func|fn|impl|type)\b"
    r"|\b(?:app|router|server)\.(?:get|post|put|patch|delete|use|listen|route)\("
    r"|@(?:\w+\.)*(?:Get|Post|Put|Delete|Patch|Request|Rest|Controller|Entity|Table|Repository|Service|route)\w*"
    r"|\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE\s+TABLE)\b"
    r"|\.(?:query|execute|findOne|findAll|save)\(|\b(?:fetch|axios\.\w+|readFile\w*|writeFile\w*|createClient|createPool)\("
    r"|\b(?:module\.exports|export\s+default)\b",
)


def choose_span(lines: list[str], max_lines: int, max_bytes: int) -> tuple[int, int]:
    """The 1-based inclusive span of a long file that holds the most definitions, routes and storage calls.

    A file that fits is returned whole. Otherwise the window starts at a block boundary (the first
    line or after a blank line), so the excerpt reads as complete blocks. Ties go to the earlier span.
    """

    count = len(lines)
    sizes = [len(line.encode("utf-8")) + 1 for line in lines]
    if count <= max_lines and sum(sizes) <= max_bytes:
        return 1, count
    signals = [1 if _SIGNAL.search(line) else 0 for line in lines]
    best_score, best = -1, (1, min(count, max_lines))
    for start in range(count):
        if start and lines[start - 1].strip():
            continue
        total = taken = score = 0
        end = start
        while end < count and taken < max_lines and total + sizes[end] <= max_bytes:
            total += sizes[end]
            score += signals[end]
            taken += 1
            end += 1
        if taken and score > best_score:
            best_score, best = score, (start + 1, end)
    return best


# ------------------------------------------------------------------ references
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;")
_JAVA_STRING = re.compile(r'''"""[\s\S]*?(?:"""|\Z)|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', re.VERBOSE)
JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"}
SUPPORTED_SUFFIXES = JS_SUFFIXES | {".py", ".java"}
_JS_RESOLVE = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".vue", ".svelte")


def extract_references(path: str, lines: list[str]) -> list[tuple[int, str, tuple[str, ...]]]:
    """``(line, specifier, imported names)`` for the static imports of JavaScript, TypeScript, Python and Java.

    Only statements that name their target in a literal are read. Dynamic paths, aliases and other
    languages produce nothing, and a commented-out line is skipped.
    """

    suffix = PurePosixPath(path).suffix.lower()
    text = "\n".join(lines)
    found: list[tuple[int, str, tuple[str, ...]]] = []
    if suffix in JS_SUFFIXES:
        from ..analyzers.javascript import parse_imports, scan_source

        source = scan_source(path, text)
        for declaration in parse_imports(source):
            token = next((item for item in source.strings if item.offset >= declaration.offset), None)
            if token is None or token.kind != "string":
                continue
            # require() and import() count only when the literal is the whole argument: './locales/' + lang is computed.
            after = source.masked[token.end:token.end + 64].lstrip()
            if declaration.kind in ("require", "dynamic") and not after.startswith(")"):
                continue
            found.append((declaration.line, declaration.specifier, ()))
    elif suffix == ".py":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.extend((node.lineno, item.name, ()) for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.append((node.lineno, "." * node.level + (node.module or ""), tuple(item.name for item in node.names)))
    elif suffix == ".java":
        from ..analyzers.java import mask_comments

        masked = _JAVA_STRING.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), mask_comments(text))
        for number, line in enumerate(masked.splitlines(), 1):
            if match := _JAVA_IMPORT.match(line):
                found.append((number, match.group(1), ()))
    return sorted((item for item in found if 0 < len(item[1]) <= MAX_SPECIFIER_LENGTH), key=lambda item: item[0])[:MAX_REFERENCES_PER_FILE]


def resolve_reference(
    source: str, specifier: str, names: tuple[str, ...], paths: set[str], java: dict[str, str] | None = None,
) -> str | None:
    """The snapshot file a literal reference names, or None. A package, alias, or missing target is None.

    ``java`` is the repository's ``java_targets`` table; pass it when resolving many references.
    """

    suffix = PurePosixPath(source).suffix.lower()
    parent = source.rsplit("/", 1)[0] if "/" in source else ""
    if suffix in JS_SUFFIXES:
        if not specifier.startswith("."):
            return None
        return _first_existing(_normalize(parent, specifier), _JS_RESOLVE, paths)
    if suffix == ".py":
        dots = len(specifier) - len(specifier.lstrip("."))
        module = specifier[dots:].replace(".", "/")
        bases = ["", "src"]
        if dots:
            base = parent
            for _ in range(dots - 1):
                base = base.rsplit("/", 1)[0] if "/" in base else ""
            bases = [base]
        for base in bases:
            stem = "/".join(part for part in (base, module) if part)
            candidates = [f"{stem}/{name}" if stem else name for name in names] + ([stem, f"{stem}/__init__"] if stem else [])
            for candidate in candidates:
                if f"{candidate}.py" in paths:
                    return f"{candidate}.py"
        return None
    if suffix == ".java":
        return (java if java is not None else java_targets(paths)).get(specifier.replace(".", "/") + ".java")
    return None


def java_targets(paths: set[str]) -> dict[str, str]:
    """Every suffix of every Java path (``com/example/Foo.java`` and shorter) mapped to its first path in sorted order.

    Built once per repository, so resolving a reference is a lookup, not a scan of every path.
    """

    table: dict[str, str] = {}
    for path in sorted(item for item in paths if item.endswith(".java")):
        parts = path.split("/")
        for start in range(len(parts)):
            table.setdefault("/".join(parts[start:]), path)
    return table


def _normalize(parent: str, specifier: str) -> str:
    parts = parent.split("/") if parent else []
    for part in specifier.split("/"):
        if part == "..":
            if not parts:
                return ""
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return "/".join(parts)


def _first_existing(stem: str, suffixes: tuple[str, ...], paths: set[str]) -> str | None:
    if not stem:
        return None
    candidates = [stem] + [stem + suffix for suffix in suffixes] + [f"{stem}/index{suffix}" for suffix in suffixes]
    return next((candidate for candidate in candidates if candidate in paths), None)
