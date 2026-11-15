"""Key-only readers for Spring style configuration files.

Only property keys are extracted. Values are never recorded, so credentials,
connection strings, and tokens in configuration cannot enter the System IR or any
model input. Constructs the readers do not understand are returned as notes for
the analyzer to report as limitations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ConfigKeys:
    keys: list[str] = field(default_factory=list)
    notes: list[tuple[int, str]] = field(default_factory=list)

    def finish(self) -> "ConfigKeys":
        self.keys = sorted(set(self.keys))
        self.notes = sorted(set(self.notes))
        return self


_PROPERTY_KEY_END = re.compile(r"(?<!\\)[=:\s]")


def read_properties_keys(text: str) -> ConfigKeys:
    result = ConfigKeys()
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line_number = index + 1
        raw = lines[index].lstrip()
        # Logical lines may continue with a trailing, unescaped backslash.
        while raw.endswith("\\") and (len(raw) - len(raw.rstrip("\\"))) % 2 == 1 and index + 1 < len(lines):
            index += 1
            raw = raw[:-1] + lines[index].lstrip()
        index += 1
        if not raw or raw.startswith("#") or raw.startswith("!"):
            continue
        match = _PROPERTY_KEY_END.search(raw)
        key = raw[: match.start()] if match else raw
        key = key.replace("\\ ", " ").replace("\\=", "=").replace("\\:", ":").strip()
        if key:
            result.keys.append(key)
        else:
            result.notes.append((line_number, "property line without a key"))
    return result.finish()


_YAML_KEY = re.compile(r"^(?P<key>\"[^\"]*\"|'[^']*'|[^\s#:\-][^:#]*?)\s*:(?:\s+(?P<value>.*)|$)")


def read_yaml_keys(text: str) -> ConfigKeys:
    """Flatten the mapping keys of a block-style YAML document into dotted paths.

    Supports nested block mappings, multiple documents separated by ``---``, comments,
    quoted keys, and block scalars. Flow collections, anchors, aliases, tags, and
    directives are reported as notes and their contents are not interpreted.
    """

    result = ConfigKeys()
    stack: list[tuple[int, str]] = []
    block_scalar_indent: int | None = None
    for line_number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if block_scalar_indent is not None:
            if stripped and indent <= block_scalar_indent:
                block_scalar_indent = None
            else:
                continue
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("---") or stripped.startswith("..."):
            stack = []
            continue
        if stripped.startswith("%"):
            result.notes.append((line_number, "YAML directive"))
            continue
        is_sequence_item = stripped == "-" or stripped.startswith("- ")
        # A sequence may sit at the same indent as its parent key, so it only closes deeper keys.
        while stack and (indent < stack[-1][0] if is_sequence_item else indent <= stack[-1][0]):
            stack.pop()
        if is_sequence_item:
            # Sequence items are the value of the enclosing key; their contents are not keys.
            if stack:
                result.keys.append(".".join(item[1] for item in stack))
            if stripped[1:].lstrip().startswith(("{", "[")):
                result.notes.append((line_number, "flow collection inside a sequence"))
            continue
        match = _YAML_KEY.match(stripped)
        if not match:
            result.notes.append((line_number, "line is not a block mapping entry"))
            continue
        key = match.group("key").strip().strip("\"'")
        value = (match.group("value") or "").strip()
        if value.startswith("#"):
            value = ""
        if key.startswith(("&", "*", "!")) or key == "<<":
            result.notes.append((line_number, "anchor, alias, tag, or merge key"))
            continue
        path = ".".join([*[item[1] for item in stack], key])
        if value.startswith(("&", "*", "!")):
            result.notes.append((line_number, "anchor, alias, or tag value"))
            result.keys.append(path)
            continue
        if value.startswith(("{", "[")):
            result.notes.append((line_number, "flow collection value"))
            result.keys.append(path)
            continue
        if value.startswith(("|", ">")):
            block_scalar_indent = indent
            result.keys.append(path)
            continue
        if value:
            result.keys.append(path)
        else:
            stack.append((indent, key))
    return result.finish()
