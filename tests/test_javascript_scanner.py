"""Structural JavaScript and TypeScript scanning used by the React adapter."""

from __future__ import annotations

from repodocs.analyzers.javascript import mask_source, parse_exports, parse_imports, scan_source, top_level_statements


def test_mask_source_keeps_offsets_and_records_literals() -> None:
    source = 'const a = "x//y"; // comment\nconst b = `base/${id}/items`;\n/* block */ const c = \'q\';\n'
    masked, strings = mask_source(source)
    assert len(masked) == len(source)
    assert "comment" not in masked and "block" not in masked
    assert [(token.kind, token.value) for token in strings] == [
        ("string", "x//y"),
        ("template", "base/${id}/items"),
        ("string", "q"),
    ]
    assert strings[1].expressions == ("id",)
    assert strings[1].pattern == "base/{id}/items"
    assert masked[strings[0].offset] == '"' and masked[strings[0].offset + 1 : strings[0].end - 1].strip() == ""


def test_jsx_apostrophes_and_quotes_in_text_are_not_strings() -> None:
    source = "const X = () => (\n  <p>Don't stop. It's \"fine\" {value}</p>\n);\nconst y = 'real';\n"
    masked, strings = mask_source(source)
    # Quotes preceded by a word are JSX text, not string literals; only operator positions start strings.
    assert [token.value for token in strings if token.kind == "string"] == ["real"]
    assert "{value}" in masked
    assert "Don't" in masked and '"fine"' in masked


def test_parse_imports_covers_every_form() -> None:
    source = (
        'import React, { useState, useEffect as effect } from "react";\n'
        "import * as utils from './utils';\n"
        'import type { Order } from "../types";\n'
        'import "./styles.css";\n'
        'export { Button } from "./Button";\n'
        'export * from "./all";\n'
        "const legacy = require('legacy-lib');\n"
        'const Lazy = React.lazy(() => import("./pages/Lazy"));\n'
        "const dynamic = import(specifier);\n"
    )
    imports = parse_imports(scan_source("index.tsx", source))
    summary = [(item.kind, item.specifier, item.line) for item in imports]
    assert summary == [
        ("static", "react", 1),
        ("static", "./utils", 2),
        ("static", "../types", 3),
        ("static", "./styles.css", 4),
        ("reexport", "./Button", 5),
        ("reexport", "./all", 6),
        ("require", "legacy-lib", 7),
        ("dynamic", "./pages/Lazy", 8),
        ("dynamic", "", 9),
    ]
    react = imports[0]
    assert react.default_name == "React"
    assert react.named == {"useState": "useState", "effect": "useEffect"}
    assert imports[1].namespace_name == "utils"
    assert imports[2].named == {"Order": "Order"}


def test_parse_exports_and_top_level_statements() -> None:
    source = (
        "export const api = axios.create({ baseURL: '/api' })\n"
        "export function Format(v) { return v }\n"
        "const Card = () => <div>{1}</div>\n"
        "export default Card\n"
        "export { Card as Alias, helper }\n"
        "class Admin extends React.Component {\n  render() { return null; }\n}\n"
    )
    named, default = parse_exports(scan_source("x.tsx", source).masked)
    assert named == {"api", "Format", "Alias", "helper"}
    assert default == "Card"
    statements = [scan_source("x.tsx", source).masked[start:end].split("\n")[0] for start, end in top_level_statements(scan_source("x.tsx", source).masked)]
    assert statements[0].startswith("export const api")
    assert statements[1].startswith("export function Format")
    assert statements[2].startswith("const Card")
    assert statements[3].startswith("export default Card")
    assert statements[-1].startswith("class Admin")
    assert len(statements) == 6
