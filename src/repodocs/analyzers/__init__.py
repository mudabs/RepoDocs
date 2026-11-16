"""The built-in analyzer contract (ADR-005, ADR-015).

An analyzer is a trusted Python module that inspects one repository snapshot and
contributes nodes, relationships, evidence, claims, and limitations to the System
IR. Analyzers read file content only; they never execute repository code, build
tools, or package managers. Every contribution carries evidence that resolves to
the snapshot revision, and anything an analyzer cannot support statically is
reported as a limitation instead of being inferred as a fact.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..common import semantic_id, sha256_bytes
from ..ir import evidence_id, node_id


CLASSIFICATION_OBSERVED = "observed"
CLASSIFICATION_INFERRED = "inferred"
CLASSIFICATION_PROPOSED = "proposed"

EVIDENCE_KIND_FILE = "file"
EVIDENCE_KIND_SPAN = "span"

BASIC_ANALYZER_NAME = "basic-manifest"

# Limitation kinds shared by all analyzers so documentation can group them.
LIMITATION_DYNAMIC_ROUTING = "dynamic-routing"
LIMITATION_REFLECTION = "reflection"
LIMITATION_GENERATED_CODE = "generated-code"
LIMITATION_RUNTIME_CONFIGURATION = "runtime-configuration"
LIMITATION_UNSUPPORTED_CONSTRUCT = "unsupported-construct"
LIMITATION_UNSUPPORTED_LANGUAGE = "unsupported-language"
LIMITATION_UNREADABLE_SOURCE = "unreadable-source"
LIMITATION_AMBIGUOUS_REFERENCE = "ambiguous-reference"
LIMITATION_KINDS = (
    LIMITATION_DYNAMIC_ROUTING,
    LIMITATION_REFLECTION,
    LIMITATION_GENERATED_CODE,
    LIMITATION_RUNTIME_CONFIGURATION,
    LIMITATION_UNSUPPORTED_CONSTRUCT,
    LIMITATION_UNSUPPORTED_LANGUAGE,
    LIMITATION_UNREADABLE_SOURCE,
    LIMITATION_AMBIGUOUS_REFERENCE,
)


@dataclass(frozen=True)
class SourceFile:
    """One discovered, text, size-limited file in the repository snapshot."""

    path: str
    size: int
    content_hash: str
    language: str | None


@dataclass
class AnalysisResult:
    """IR contributions from one analyzer run. Lists are merged and sorted by the scanner."""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[dict[str, Any]] = field(default_factory=list)

    def extend(self, other: "AnalysisResult") -> None:
        self.nodes.extend(other.nodes)
        self.relationships.extend(other.relationships)
        self.evidence.extend(other.evidence)
        self.claims.extend(other.claims)
        self.limitations.extend(other.limitations)


class RepositoryContext:
    """Read-only view of one repository snapshot handed to analyzers.

    File text is read on demand and checked against the hash recorded at discovery
    time, so an analyzer can never attach evidence to content the snapshot did not
    observe. Decoding failures and changed files surface as ``text()`` returning
    ``None``; the analyzer decides how to report that as a limitation.
    """

    def __init__(
        self,
        *,
        repository_id: str,
        root: Path,
        files: list[SourceFile],
        revision: str,
        revision_kind: str,
        analyzer_name: str = BASIC_ANALYZER_NAME,
    ) -> None:
        self.repository_id = repository_id
        self.root = root
        self.files = sorted(files, key=lambda item: item.path)
        self.revision = revision
        self.revision_kind = revision_kind
        self.analyzer_name = analyzer_name
        self.repository_node_id = node_id("repository", repository_id)
        self._files_by_path = {item.path: item for item in self.files}
        self._text_cache: dict[str, str | None] = {}
        self._line_starts: dict[str, list[int]] = {}

    def file(self, path: str) -> SourceFile | None:
        return self._files_by_path.get(path)

    def files_with_suffix(self, *suffixes: str) -> list[SourceFile]:
        lowered = tuple(suffix.lower() for suffix in suffixes)
        return [item for item in self.files if item.path.lower().endswith(lowered)]

    def files_named(self, *names: str) -> list[SourceFile]:
        lowered = {name.lower() for name in names}
        return [item for item in self.files if item.path.rsplit("/", 1)[-1].lower() in lowered]

    def text(self, path: str) -> str | None:
        """UTF-8 text of a discovered file, or ``None`` when it changed or cannot be decoded."""

        if path in self._text_cache:
            return self._text_cache[path]
        source = self._files_by_path.get(path)
        result: str | None = None
        if source is not None:
            try:
                data = (self.root / path).read_bytes()
            except OSError:
                data = None
            if data is not None and sha256_bytes(data) == source.content_hash:
                try:
                    result = data.decode("utf-8")
                except UnicodeDecodeError:
                    result = None
        self._text_cache[path] = result
        return result

    def line_of(self, path: str, offset: int) -> int:
        """1-based line number of a character offset inside ``text(path)``."""

        if path not in self._line_starts:
            text = self.text(path) or ""
            starts = [0]
            for index, character in enumerate(text):
                if character == "\n":
                    starts.append(index + 1)
            self._line_starts[path] = starts
        return bisect.bisect_right(self._line_starts[path], offset)

    def file_evidence(self, path: str) -> dict[str, Any]:
        """The whole-file evidence record the basic scanner also emits for ``path``."""

        source = self._files_by_path[path]
        return {
            "id": evidence_id(self.repository_id, path, source.content_hash),
            "repository_id": self.repository_id,
            "path": path,
            "content_hash": source.content_hash,
            "revision": self.revision,
            "revision_kind": self.revision_kind,
            "kind": EVIDENCE_KIND_FILE,
            "analyzer": BASIC_ANALYZER_NAME,
        }

    def span_evidence(
        self, path: str, line_start: int, line_end: int | None = None, *, symbol: str | None = None
    ) -> dict[str, Any]:
        """Evidence for a line range inside a file, tied to the same revision as the file."""

        source = self._files_by_path[path]
        end = line_end if line_end is not None and line_end >= line_start else line_start
        locator = f"L{line_start}-L{end}"
        return {
            "id": evidence_id(self.repository_id, path, source.content_hash, locator, symbol or ""),
            "repository_id": self.repository_id,
            "path": path,
            "content_hash": source.content_hash,
            "revision": self.revision,
            "revision_kind": self.revision_kind,
            "kind": EVIDENCE_KIND_SPAN,
            "line_start": line_start,
            "line_end": end,
            "symbol": symbol,
            "analyzer": self.analyzer_name,
        }

    def limitation(self, kind: str, message: str, evidence_ids: list[str]) -> dict[str, Any]:
        if kind not in LIMITATION_KINDS:
            raise ValueError(f"Unknown limitation kind: {kind}")
        ordered = sorted(set(evidence_ids))
        return {
            "id": semantic_id("limitation", self.repository_id, self.analyzer_name, kind, message, *ordered),
            "repository_id": self.repository_id,
            "analyzer": self.analyzer_name,
            "kind": kind,
            "message": message,
            "evidence_ids": ordered,
        }


class Analyzer(Protocol):
    """A trusted built-in analyzer. ``detect`` is cheap; ``analyze`` runs only when it returns true."""

    name: str
    version: str

    def detect(self, context: RepositoryContext) -> bool: ...

    def analyze(self, context: RepositoryContext) -> AnalysisResult: ...


def run_analyzers(
    context: RepositoryContext, analyzers: tuple[Analyzer, ...]
) -> tuple[AnalysisResult, list[dict[str, Any]]]:
    """Run every analyzer whose detection succeeds and report which ones ran."""

    merged = AnalysisResult()
    report: list[dict[str, Any]] = []
    for analyzer in sorted(analyzers, key=lambda item: item.name):
        context.analyzer_name = analyzer.name
        detected = bool(analyzer.detect(context))
        entry: dict[str, Any] = {"name": analyzer.name, "version": analyzer.version, "detected": detected}
        if detected:
            result = analyzer.analyze(context)
            merged.extend(result)
            entry["nodes"] = len({item["id"] for item in result.nodes})
            entry["limitations"] = len({item["id"] for item in result.limitations})
        report.append(entry)
    context.analyzer_name = BASIC_ANALYZER_NAME
    # Imported here because the linking step builds on the contract types above.
    from .orm import link_orm_metadata

    report.append(link_orm_metadata(context, merged))
    return merged, report


def builtin_analyzers() -> tuple[Analyzer, ...]:
    """The trusted analyzers shipped with RepoDocs, in a stable order."""

    from .openapi import OpenApiAnalyzer
    from .postgresql import PostgreSqlAnalyzer
    from .react import ReactAnalyzer
    from .spring_boot import SpringBootAnalyzer

    return (OpenApiAnalyzer(), PostgreSqlAnalyzer(), ReactAnalyzer(), SpringBootAnalyzer())
