"""Static helpers that choose spans, classify layers and read literal imports (ADR-029)."""

from __future__ import annotations

import pytest

from repodocs.architecture.source_index import (
    MAX_REFERENCES_PER_FILE, choose_span, extract_references, is_asset, is_noise, is_vendored, java_targets, layer_of, resolve_reference,
)


# ------------------------------------------------------------------ references
def test_javascript_imports_requires_reexports_and_dynamic_imports_are_found_and_comments_are_not() -> None:
    lines = [
        "import express from 'express';",
        "import { a, b } from './lib/a';",
        "import './side-effect';",
        "export * from './reexport';",
        "const c = require('./c');",
        "const lazy = () => import('./lazy');",
        "import {",
        "  d,",
        "} from './multi-line';",
        "// import hidden from './hidden';",
        "/* require('./doc-comment'); */",
        "const name = 'not-a-reference';",
    ]

    found = extract_references("src/app.js", lines)

    assert [(line, specifier) for line, specifier, _ in found] == [
        (1, "express"), (2, "./lib/a"), (3, "./side-effect"), (4, "./reexport"), (5, "./c"), (6, "./lazy"), (7, "./multi-line"),
    ]


def test_python_and_java_imports_are_found() -> None:
    python = ["import os, json", "from . import util, db as database", "from pkg.sub import thing  # comment", "# import hidden", "x = 'import nothing'"]
    java = ["package shop;", "import shop.orders.OrderRepository;", "import static shop.util.Strings.join;", "import java.util.*;", "// import shop.hidden.Hidden;"]

    assert [(line, spec) for line, spec, _ in extract_references("a.py", python)] == [(1, "os"), (1, "json"), (2, "."), (3, "pkg.sub")]
    assert extract_references("a.py", python)[2][2] == ("util", "db")
    assert [(line, spec) for line, spec, _ in extract_references("A.java", java)] == [
        (2, "shop.orders.OrderRepository"), (3, "shop.util.Strings.join"), (4, "java.util"),
    ]


def test_other_languages_and_dynamic_paths_yield_nothing() -> None:
    assert extract_references("main.rs", ["use crate::other;", "mod other;"]) == []
    assert extract_references("a.js", ["require(name);", "import(`./x/${y}`);", "const s = 'x';"]) == []


def test_a_computed_require_or_import_names_no_file() -> None:
    computed = ["const a = require('./plugins/' + name);", "const b = import('./pages/' + page);", "const c = require('./x'.concat(y));"]

    assert extract_references("a.js", computed) == []
    assert [spec for _, spec, _ in extract_references("a.js", ["const d = require( './plain' );"])] == ["./plain"]


@pytest.mark.parametrize("path,text", [
    ("a.js", "/*\nimport fake from './fake';\nrequire('./fake');\n*/"),
    ("a.js", 'const example = "require(\'./fake\')";'),
    ("a.js", "const example = `\nimport fake from './fake';\n`;"),
    ("a.py", '\"\"\"\nimport fake\nfrom fake import secret\n\"\"\"'),
    ("A.java", "/*\nimport fake.Hidden;\n*/"),
    ("A.java", 'String example = \"\"\"\nimport fake.Hidden;\n\"\"\";'),
    ("A.java", 'String example = \"\"\"\nimport fake.Hidden;'),
])
def test_comments_and_string_contents_are_not_indexed_as_imports(path: str, text: str) -> None:
    assert extract_references(path, text.splitlines()) == []


def test_references_per_file_and_specifier_length_are_bounded() -> None:
    many = [f"require('./m{index}');" for index in range(200)]
    long = ["require('./" + "a" * 500 + "');"]

    assert len(extract_references("a.js", many)) == MAX_REFERENCES_PER_FILE
    assert extract_references("a.js", long) == []


PATHS = {
    "src/app.js", "src/lib/a.js", "src/lib/index.js", "src/ui/Widget.tsx", "pkg/__init__.py", "pkg/util.py", "pkg/sub/__init__.py",
    "src/pkg2/mod.py", "main/java/shop/orders/OrderRepository.java",
}


@pytest.mark.parametrize(
    ("source", "specifier", "names", "expected"),
    [
        ("src/app.js", "./lib/a", (), "src/lib/a.js"),
        ("src/app.js", "./lib", (), "src/lib/index.js"),
        ("src/app.js", "./ui/Widget", (), "src/ui/Widget.tsx"),
        ("src/app.js", "express", (), None),
        ("src/app.js", "../../../outside", (), None),
        ("src/app.js", "./missing", (), None),
        ("pkg/util.py", ".", ("util",), "pkg/util.py"),
        ("pkg/util.py", ".sub", (), "pkg/sub/__init__.py"),
        ("app.py", "pkg.util", (), "pkg/util.py"),
        ("app.py", "pkg", ("util",), "pkg/util.py"),
        ("app.py", "pkg2.mod", (), "src/pkg2/mod.py"),
        ("app.py", "os", (), None),
        ("main/java/shop/Main.java", "shop.orders.OrderRepository", (), "main/java/shop/orders/OrderRepository.java"),
        ("main/java/shop/Main.java", "java.util.List", (), None),
        ("notes.txt", "./a", (), None),
    ],
)
def test_a_reference_resolves_only_to_a_file_in_the_snapshot(source: str, specifier: str, names: tuple[str, ...], expected: str | None) -> None:
    assert resolve_reference(source, specifier, names, PATHS) == expected
    assert resolve_reference(source, specifier, names, PATHS, java_targets(PATHS)) == expected


def test_a_java_class_resolves_by_table_to_the_first_matching_path_in_sorted_order() -> None:
    paths = {f"module{index}/src/main/java/shop/orders/Order{index}.java" for index in range(3000)}
    paths |= {"b/java/shop/orders/Shared.java", "a/java/shop/orders/Shared.java", "a/java/other/shop/orders/Shared.java"}
    table = java_targets(paths)

    assert resolve_reference("X.java", "shop.orders.Shared", (), paths, table) == "a/java/other/shop/orders/Shared.java"
    assert resolve_reference("X.java", "shop.orders.Order2999", (), paths, table) == "module2999/src/main/java/shop/orders/Order2999.java"
    assert resolve_reference("X.java", "shop.orders.Missing", (), paths, table) is None
    assert table["shop/orders/Shared.java"] == min(path for path in paths if path.endswith("/shop/orders/Shared.java"))


# ------------------------------------------------------------------ layers and classes of path
@pytest.mark.parametrize(
    ("path", "layer"),
    [
        ("boneset-api/server.js", "backend"),
        ("templates/js/api.js", "frontend"),
        ("public/app.js", "frontend"),
        ("src/App.jsx", "frontend"),
        ("src/main/java/shop/OrderRepository.java", "storage"),
        ("src/main/java/shop/OrderController.java", "backend"),
        ("db/migrations/001.sql", "storage"),
        ("models/user.py", "storage"),
        ("lib/util.js", "other"),
    ],
)
def test_a_layer_comes_from_the_name_and_location_of_a_path(path: str, layer: str) -> None:
    assert layer_of(path) == layer


def test_assets_vendored_directories_and_test_files_are_recognized() -> None:
    assert is_asset("images/a.PNG") and is_asset("fonts/x.woff2") and not is_asset("src/a.js")
    assert is_vendored("node_modules/x/index.js") and is_vendored("a/dist/b.js") and not is_vendored("src/dist.js")
    assert is_noise("tests/a.js") and is_noise("src/test_a.py") and not is_noise("src/a.js")


# ------------------------------------------------------------------ spans
def test_a_file_that_fits_is_returned_whole() -> None:
    assert choose_span(["a", "b", "c"], 200, 8192) == (1, 3)
    assert choose_span([], 200, 8192) == (1, 0)


def test_a_long_file_yields_the_block_aligned_window_with_the_most_definitions() -> None:
    filler = [f"const value{index} = {index};" if index % 10 else "" for index in range(300)]
    routes = ["app.get('/a', () => {", "  return fs.readFileSync('x');", "});", "", "app.post('/b', save);"]
    lines = filler + routes

    start, end = choose_span(lines, 50, 8192)

    assert end - start + 1 <= 50 and end == len(lines), "the window reaches the routes"
    assert start == 1 or lines[start - 2].strip() == "", "the window starts at a block boundary"
    assert choose_span(lines, 50, 8192) == (start, end), "the choice is deterministic"


def test_the_window_never_exceeds_the_byte_bound_and_ties_go_to_the_earlier_span() -> None:
    lines = ["x" * 99 if index % 5 else "" for index in range(100)]

    start, end = choose_span(lines, 200, 1000)

    assert sum(len(line.encode()) + 1 for line in lines[start - 1 : end]) <= 1000
    assert start == 1, "with no signals anywhere the first span wins"
