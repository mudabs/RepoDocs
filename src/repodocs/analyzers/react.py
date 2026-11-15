"""React framework adapter.

Extracts, from source text only, the facts documentation needs about a React
front end: the application package and its toolchain, source modules and their
import graph, components, routes declared with React Router or by Next.js file
conventions, and the HTTP request sites that name a literal URL. Component to
component references are recorded only for JSX elements that resolve to a
component defined in the same repository; arbitrary function calls are never
turned into relationships.

Everything that depends on runtime behaviour is reported as a limitation rather
than extracted: ``import()`` and ``React.lazy`` loads, request URLs and route
paths that are not literals, import specifiers that resolve to neither a file nor
a declared dependency, and dynamic code evaluation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..ir import claim_id, node_id, relationship_id
from . import (
    CLASSIFICATION_OBSERVED,
    LIMITATION_AMBIGUOUS_REFERENCE,
    LIMITATION_DYNAMIC_ROUTING,
    LIMITATION_REFLECTION,
    LIMITATION_RUNTIME_CONFIGURATION,
    LIMITATION_UNREADABLE_SOURCE,
    LIMITATION_UNSUPPORTED_CONSTRUCT,
    AnalysisResult,
    RepositoryContext,
    SourceFile,
)
from .javascript import (
    ImportDecl,
    JsSource,
    object_fields,
    parse_exports,
    parse_imports,
    scan_source,
    skip_balanced,
    split_top_level,
    top_level_statements,
)


FRAMEWORK_NAME = "React"
MODULE_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
RESOLVE_SUFFIXES = (".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".json")
SKIPPED_SEGMENTS = {"__tests__", "__mocks__", "cypress", "e2e", "coverage", "public", ".next", "storybook-static"}
SKIPPED_NAME = re.compile(r"(\.(test|spec|stories|cy)\.[cm]?[jt]sx?$)|(\.d\.ts$)|(\.config\.[cm]?[jt]s$)|(^\.)")
TOOLCHAIN_PACKAGES = ("next", "vite", "react-scripts", "webpack", "parcel", "@remix-run/react", "gatsby", "expo")
ROUTER_PACKAGES = ("react-router-dom", "react-router", "@tanstack/react-router", "wouter", "next")
HTTP_CLIENT_PACKAGES = (
    "axios",
    "ky",
    "superagent",
    "@tanstack/react-query",
    "swr",
    "graphql-request",
    "@apollo/client",
    "urql",
)
NODE_BUILTINS = {
    "assert", "buffer", "child_process", "crypto", "dns", "events", "fs", "http", "https", "net", "os",
    "path", "process", "querystring", "readline", "stream", "tls", "url", "util", "worker_threads", "zlib",
}
HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")
COMPONENT_WRAPPERS = ("memo", "React.memo", "forwardRef", "React.forwardRef", "observer", "withRouter")
CLASS_BASES = {"Component", "React.Component", "PureComponent", "React.PureComponent"}
JSX_ELEMENT = re.compile(r"<([A-Z][\w$]*(?:\.[\w$]+)*)(?=[\s/>])")
JSX_ANY = re.compile(r"<[A-Za-z][\w$.-]*(?:\s+[\w$-]+\s*=|\s*/?>|\s*>)|React\.createElement\s*\(|(?<![\w$])_?jsxs?\s*\(")
FUNCTION_COMPONENT = re.compile(r"(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Z][\w$]*)\s*[(<]")
CONST_COMPONENT = re.compile(r"(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Z][\w$]*)\s*(?::[^=]{0,80}?)?=\s*(?P<init>[^\n]{0,80})")
CLASS_COMPONENT = re.compile(r"(?:export\s+(?:default\s+)?)?class\s+(?P<name>[A-Z][\w$]*)\s+extends\s+(?P<base>[\w.]+)")
ROUTE_OPEN = re.compile(r"<Route(?=[\s/>])")
ROUTE_CLOSE = re.compile(r"</Route\s*>")
ROUTE_CALLS = re.compile(r"\b(createBrowserRouter|createHashRouter|createMemoryRouter|useRoutes)\s*\(")
FETCH_CALL = re.compile(r"(?<![\w$.])(?:window\.|globalThis\.)?fetch\s*\(")
AXIOS_CALL = re.compile(r"(?<![\w$.])axios\s*\(")
INSTANCE_CREATE = re.compile(r"(?:const|let|var)\s+([\w$]+)\s*=\s*(axios\s*\.\s*create|ky\s*\.\s*(?:create|extend))\s*\(")
EVAL_CALL = re.compile(r"(?<![\w$.])(?:eval\s*\(|new\s+Function\s*\()")
LAZY_CALL = re.compile(r"(?<![\w$.])(?:React\s*\.\s*)?lazy\s*\(")
NEXT_ROUTE_HANDLER = re.compile(r"\bexport\s+(?:async\s+)?(?:function\s+|const\s+)(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b")


@dataclass
class Package:
    path: str  # package.json path
    directory: str  # "" for the repository root
    name: str
    react_version: str | None
    dependencies: set[str]
    toolchain: list[str]
    routers: list[str]
    http_clients: list[str]
    typescript: bool
    node_id: str = ""
    evidence_id: str = ""


@dataclass
class Component:
    name: str
    module: str
    line: int
    end_line: int
    kind: str
    body_start: int
    body_end: int
    node_id: str = ""
    evidence_id: str = ""
    exported: bool = False
    default_export: bool = False


@dataclass
class Module:
    source: JsSource
    file: SourceFile
    node_id: str
    evidence_id: str
    package: Package | None
    imports: list[ImportDecl] = field(default_factory=list)
    components: dict[str, Component] = field(default_factory=dict)
    named_exports: set[str] = field(default_factory=set)
    default_export: str | None = None
    resolved_imports: dict[str, str] = field(default_factory=dict)  # specifier -> module path


class ReactAnalyzer:
    name = "react"
    version = "1.0"

    # ----------------------------------------------------------------- detection
    def detect(self, context: RepositoryContext) -> bool:
        return self._detected(context, self._packages(context), self._module_files(context))

    def _detected(self, context: RepositoryContext, packages: list[Package], module_files: list[SourceFile]) -> bool:
        """React is detected when a package declares react and has source modules, or a module is React source."""

        for package in packages:
            prefix = package.directory + "/" if package.directory else ""
            if any(source.path.startswith(prefix) for source in module_files):
                return True
        return any(self._is_react_source(context, source) for source in module_files)

    def _packages(self, context: RepositoryContext) -> list[Package]:
        packages: list[Package] = []
        for source in context.files_named("package.json"):
            text = context.text(source.path)
            if text is None:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            dependencies: dict[str, str] = {}
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                values = data.get(section)
                if isinstance(values, dict):
                    dependencies.update({str(key): str(value) for key, value in values.items()})
            if "react" not in dependencies:
                continue
            directory = source.path.rsplit("/", 1)[0] if "/" in source.path else ""
            packages.append(
                Package(
                    path=source.path,
                    directory=directory,
                    name=str(data.get("name") or (directory.rsplit("/", 1)[-1] if directory else context.repository_id)),
                    react_version=dependencies.get("react") or None,
                    dependencies=set(dependencies),
                    toolchain=[item for item in TOOLCHAIN_PACKAGES if item in dependencies],
                    routers=[item for item in ROUTER_PACKAGES if item in dependencies],
                    http_clients=[item for item in HTTP_CLIENT_PACKAGES if item in dependencies],
                    typescript="typescript" in dependencies or source.path.replace("package.json", "tsconfig.json") in {f.path for f in context.files},
                )
            )
        return sorted(packages, key=lambda item: item.path)

    @staticmethod
    def _module_files(context: RepositoryContext) -> list[SourceFile]:
        result = []
        for source in context.files_with_suffix(*MODULE_SUFFIXES):
            segments = source.path.split("/")
            if any(segment in SKIPPED_SEGMENTS for segment in segments[:-1]):
                continue
            if SKIPPED_NAME.search(segments[-1]):
                continue
            result.append(source)
        return result

    @staticmethod
    def _is_react_source(context: RepositoryContext, source: SourceFile) -> bool:
        if source.path.endswith((".jsx", ".tsx")):
            return True
        text = context.text(source.path)
        return text is not None and re.search(r"""from\s+["']react["']|require\s*\(\s*["']react["']\s*\)""", text) is not None

    # ------------------------------------------------------------------ analysis
    def analyze(self, context: RepositoryContext) -> AnalysisResult:
        result = AnalysisResult()
        repository = context.repository_id
        packages = self._packages(context)
        module_files = self._module_files(context)
        if not self._detected(context, packages, module_files):
            return result

        framework_node = node_id("framework", repository, "react")
        manifest_evidence = [context.file_evidence(package.path) for package in packages]
        result.evidence.extend(manifest_evidence)
        version = next((package.react_version for package in packages if package.react_version), None)
        result.nodes.append(
            {
                "id": framework_node,
                "type": "framework",
                "name": FRAMEWORK_NAME,
                "attributes": {
                    "repository_id": repository,
                    "version": version,
                    "version_kind": "declared-range" if version else None,
                    "manifests": [package.path for package in packages],
                },
                "evidence_ids": [item["id"] for item in manifest_evidence],
            }
        )
        result.relationships.append(
            self._edge("depends_on", context.repository_node_id, framework_node, [item["id"] for item in manifest_evidence], {"framework": FRAMEWORK_NAME})
        )
        for package in packages:
            self._service(context, package, result)
        if not packages:
            result.limitations.append(
                context.limitation(
                    LIMITATION_UNSUPPORTED_CONSTRUCT,
                    "React was detected from source imports only; no package.json declares react, so the package, "
                    "toolchain, and version are unknown and modules are attached to the repository.",
                    [],
                )
            )

        modules = self._modules(context, packages, module_files, result)
        self._resolve_imports(context, modules, result)
        self._components(context, modules, result)
        self._routes(context, modules, result)
        self._next_routes(context, modules, result)
        self._requests(context, modules, result)
        self._scan_limitations(context, modules, result)
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

    # ------------------------------------------------------- extraction steps
    def _service(self, context: RepositoryContext, package: Package, result: AnalysisResult) -> None:
        evidence = context.file_evidence(package.path)
        package.evidence_id = evidence["id"]
        package.node_id = node_id("service", context.repository_id, package.path)
        result.nodes.append(
            {
                "id": package.node_id,
                "type": "service",
                "name": package.name,
                "attributes": {
                    "repository_id": context.repository_id,
                    "framework": FRAMEWORK_NAME,
                    "framework_version": package.react_version,
                    "package_name": package.name,
                    "package_path": package.path,
                    "package_directory": package.directory,
                    "toolchain": package.toolchain,
                    "routers": package.routers,
                    "http_clients": package.http_clients,
                    "typescript": package.typescript,
                },
                "evidence_ids": [evidence["id"]],
            }
        )
        result.relationships.append(self._edge("contains", context.repository_node_id, package.node_id, [evidence["id"]]))
        version = f" {package.react_version}" if package.react_version else ""
        result.claims.append(
            self._claim(
                f"{package.path} declares React{version} for package {package.name} in repository {context.repository_id}.",
                [evidence["id"]],
            )
        )

    def _modules(
        self,
        context: RepositoryContext,
        packages: list[Package],
        module_files: list[SourceFile],
        result: AnalysisResult,
    ) -> dict[str, Module]:
        modules: dict[str, Module] = {}
        for source in module_files:
            text = context.text(source.path)
            evidence = context.file_evidence(source.path)
            result.evidence.append(evidence)
            if text is None:
                result.limitations.append(
                    context.limitation(
                        LIMITATION_UNREADABLE_SOURCE,
                        f"{source.path} could not be decoded as UTF-8 or changed during the scan; it was not analyzed.",
                        [evidence["id"]],
                    )
                )
                continue
            owner = None
            for package in packages:
                if package.directory == "" or source.path.startswith(package.directory + "/"):
                    if owner is None or len(package.directory) > len(owner.directory):
                        owner = package
            module = Module(
                source=scan_source(source.path, text),
                file=source,
                node_id=node_id("module", context.repository_id, source.path),
                evidence_id=evidence["id"],
                package=owner,
            )
            module.imports = parse_imports(module.source)
            module.named_exports, module.default_export = parse_exports(module.source.masked)
            modules[source.path] = module
            language = "TypeScript" if source.path.endswith((".ts", ".tsx")) else "JavaScript"
            result.nodes.append(
                {
                    "id": module.node_id,
                    "type": "module",
                    "name": source.path,
                    "attributes": {
                        "repository_id": context.repository_id,
                        "path": source.path,
                        "language": language,
                        "jsx": source.path.endswith((".jsx", ".tsx")),
                        "package": owner.name if owner else None,
                        "imports": sorted({item.specifier for item in module.imports if item.specifier}),
                        "named_exports": sorted(module.named_exports),
                        "default_export": module.default_export,
                    },
                    "evidence_ids": [evidence["id"]],
                }
            )
            parent = owner.node_id if owner else context.repository_node_id
            result.relationships.append(self._edge("contains", parent, module.node_id, [evidence["id"]]))
        return modules

    def _resolve_imports(self, context: RepositoryContext, modules: dict[str, Module], result: AnalysisResult) -> None:
        paths = {module.file.path for module in modules.values()}
        all_files = {item.path for item in context.files}
        for module in modules.values():
            unresolved: list[tuple[ImportDecl, str]] = []
            dynamic: list[ImportDecl] = []
            for decl in module.imports:
                if decl.kind == "dynamic":
                    dynamic.append(decl)
                    if not decl.specifier:
                        span = context.span_evidence(module.file.path, decl.line)
                        result.evidence.append(span)
                        result.limitations.append(
                            context.limitation(
                                LIMITATION_UNSUPPORTED_CONSTRUCT,
                                f"{module.file.path} line {decl.line}: import() specifier is not a string literal; the loaded module is unknown.",
                                [span["id"]],
                            )
                        )
                        continue
                if not decl.specifier:
                    continue
                span = context.span_evidence(module.file.path, decl.line)
                attributes = {"kind": decl.kind, "specifier": decl.specifier}
                if decl.specifier.startswith((".", "/")):
                    target = _resolve_relative(module.file.path, decl.specifier, all_files)
                    if target is None:
                        unresolved.append((decl, "relative import does not match a discovered file"))
                        continue
                    module.resolved_imports[decl.specifier] = target
                    if target in paths:
                        result.evidence.append(span)
                        result.relationships.append(
                            self._edge("imports", module.node_id, modules[target].node_id, [span["id"]], attributes)
                        )
                    continue
                package_name = _package_name(decl.specifier)
                if package_name.startswith("node:") or package_name in NODE_BUILTINS:
                    continue
                declared = module.package is not None and package_name in module.package.dependencies
                if declared:
                    result.evidence.append(span)
                    result.relationships.append(
                        self._edge(
                            "imports",
                            module.node_id,
                            node_id("dependency", context.repository_id, package_name),
                            [span["id"]],
                            attributes,
                        )
                    )
                else:
                    unresolved.append((decl, "neither a relative file nor a declared dependency"))
            if unresolved:
                spans = [context.span_evidence(module.file.path, decl.line) for decl, _ in unresolved]
                result.evidence.extend(spans)
                specifiers = ", ".join(sorted({decl.specifier for decl, _ in unresolved}))
                result.limitations.append(
                    context.limitation(
                        LIMITATION_AMBIGUOUS_REFERENCE,
                        f"{module.file.path}: {len(unresolved)} import specifier(s) could not be resolved to a file or a "
                        f"declared dependency and were not linked: {specifiers}. Path aliases are not applied.",
                        [span["id"] for span in spans],
                    )
                )
            literal_dynamic = [decl for decl in dynamic if decl.specifier]
            if literal_dynamic:
                spans = [context.span_evidence(module.file.path, decl.line) for decl in literal_dynamic]
                result.evidence.extend(spans)
                specifiers = ", ".join(sorted({decl.specifier for decl in literal_dynamic}))
                result.limitations.append(
                    context.limitation(
                        LIMITATION_RUNTIME_CONFIGURATION,
                        f"{module.file.path}: {len(literal_dynamic)} module(s) are loaded at runtime with import(): "
                        f"{specifiers}. The import edge is recorded; whether and when the load happens is not.",
                        [span["id"] for span in spans],
                    )
                )

    def _components(self, context: RepositoryContext, modules: dict[str, Module], result: AnalysisResult) -> None:
        # Pass 1: declarations.
        for module in modules.values():
            masked = module.source.masked
            for start, end in top_level_statements(masked):
                statement = masked[start:end]
                component = self._component_declaration(statement, start, end, module)
                if component is None:
                    continue
                component.node_id = node_id("component", context.repository_id, module.file.path, component.name)
                evidence = context.span_evidence(module.file.path, component.line, component.end_line, symbol=component.name)
                result.evidence.append(evidence)
                component.evidence_id = evidence["id"]
                component.exported = component.name in module.named_exports or module.default_export == component.name
                component.default_export = module.default_export == component.name
                module.components[component.name] = component
                result.nodes.append(
                    {
                        "id": component.node_id,
                        "type": "component",
                        "name": component.name,
                        "attributes": {
                            "repository_id": context.repository_id,
                            "module": module.file.path,
                            "line": component.line,
                            "kind": component.kind,
                            "exported": component.exported,
                            "default_export": component.default_export,
                        },
                        "evidence_ids": [evidence["id"]],
                    }
                )
                result.relationships.append(self._edge("contains", module.node_id, component.node_id, [evidence["id"]]))
                result.claims.append(
                    self._claim(f"{component.name} is a React {component.kind} component defined in {module.file.path}.", [evidence["id"]])
                )
        # Pass 2: JSX references between components defined in this repository.
        for module in modules.values():
            for component in module.components.values():
                body = module.source.masked[component.body_start : component.body_end]
                referenced = sorted({match.group(1).split(".")[0] for match in JSX_ELEMENT.finditer(body)})
                for name in referenced:
                    target = self._resolve_component(name, module, modules)
                    if target is None or target.node_id == component.node_id:
                        continue
                    result.relationships.append(
                        self._edge(
                            "references",
                            component.node_id,
                            target.node_id,
                            [component.evidence_id],
                            {"kind": "jsx-element", "name": name},
                        )
                    )

    def _component_declaration(self, statement: str, start: int, end: int, module: Module) -> Component | None:
        line = module.source.line(start)
        end_line = module.source.line(max(end - 1, start))
        match = CLASS_COMPONENT.match(statement)
        if match and match.group("base") in CLASS_BASES:
            return Component(match.group("name"), module.file.path, line, end_line, "class", start, end)
        match = FUNCTION_COMPONENT.match(statement)
        if match and JSX_ANY.search(statement):
            return Component(match.group("name"), module.file.path, line, end_line, "function", start, end)
        match = CONST_COMPONENT.match(statement)
        if match:
            init = match.group("init").strip()
            kind = None
            if init.startswith(("(", "async", "function", "<")):
                kind = "function"
            for wrapper in COMPONENT_WRAPPERS:
                if init.startswith(wrapper + "(") or init.startswith(wrapper + " ("):
                    kind = wrapper.split(".")[-1]
            if kind and JSX_ANY.search(statement):
                return Component(match.group("name"), module.file.path, line, end_line, kind, start, end)
        return None

    def _resolve_component(self, name: str, module: Module, modules: dict[str, Module]) -> Component | None:
        if name in module.components:
            return module.components[name]
        for decl in module.imports:
            if decl.kind not in ("static", "dynamic"):
                continue
            target_path = module.resolved_imports.get(decl.specifier)
            target = modules.get(target_path) if target_path else None
            if target is None:
                continue
            if decl.default_name == name and target.default_export in target.components:
                return target.components[target.default_export]
            imported = decl.named.get(name)
            if imported and imported in target.components:
                return target.components[imported]
        return None

    # ------------------------------------------------------------------- routes
    def _routes(self, context: RepositoryContext, modules: dict[str, Module], result: AnalysisResult) -> None:
        for module in modules.values():
            masked = module.source.masked
            if "react-router" not in " ".join(item.specifier for item in module.imports) and not ROUTE_OPEN.search(masked):
                continue
            self._jsx_routes(context, module, modules, result)
            self._object_routes(context, module, modules, result)

    def _jsx_routes(self, context: RepositoryContext, module: Module, modules: dict[str, Module], result: AnalysisResult) -> None:
        masked = module.source.masked
        events: list[tuple[int, str, int]] = []
        for match in ROUTE_OPEN.finditer(masked):
            tag_end = _tag_end(masked, match.end())
            events.append((match.start(), "open", tag_end))
        for match in ROUTE_CLOSE.finditer(masked):
            events.append((match.start(), "close", match.end()))
        events.sort()
        stack: list[str | None] = []
        for offset, kind, tag_end in events:
            if kind == "close":
                if stack:
                    stack.pop()
                continue
            attributes_text = masked[offset + len("<Route") : tag_end]
            self_closing = attributes_text.rstrip().endswith("/>")
            path, dynamic = _jsx_attribute_string(module.source, offset + len("<Route"), tag_end, "path")
            is_index = re.search(r"\bindex(?:\s*=\s*\{\s*true\s*\})?(?=[\s/>])", attributes_text) is not None
            component = _jsx_component_attribute(attributes_text)
            line = module.source.line(offset)
            end_line = module.source.line(max(tag_end - 1, offset))
            parent = next((item for item in reversed(stack) if item is not None), "")
            if dynamic:
                span = context.span_evidence(module.file.path, line, end_line)
                result.evidence.append(span)
                result.limitations.append(
                    context.limitation(
                        LIMITATION_DYNAMIC_ROUTING,
                        f"{module.file.path} line {line}: <Route> path is not a string literal, so the route was not extracted.",
                        [span["id"]],
                    )
                )
                full_path = None
            elif path is None and not is_index:
                full_path = parent or None  # layout route without a path
            else:
                full_path = _join_route(parent, path or "")
            if full_path is not None and (path is not None or is_index):
                self._route_node(context, module, modules, result, full_path, path, component, "react-router", line, end_line, is_index)
            if not self_closing:
                stack.append(full_path)

    def _object_routes(self, context: RepositoryContext, module: Module, modules: dict[str, Module], result: AnalysisResult) -> None:
        masked = module.source.masked
        for match in ROUTE_CALLS.finditer(masked):
            args_end = skip_balanced(masked, match.end() - 1, "(", ")")
            array_start = masked.find("[", match.end(), args_end)
            if array_start == -1:
                continue
            array_end = skip_balanced(masked, array_start, "[", "]")
            self._object_route_array(context, module, modules, result, array_start, array_end, "")

    def _object_route_array(
        self,
        context: RepositoryContext,
        module: Module,
        modules: dict[str, Module],
        result: AnalysisResult,
        array_start: int,
        array_end: int,
        parent: str,
    ) -> None:
        masked = module.source.masked
        for item_start, item_end in split_top_level(masked, array_start + 1, array_end - 1):
            brace = masked.find("{", item_start, item_end)
            if brace == -1:
                continue
            object_end = skip_balanced(masked, brace, "{", "}")
            fields = object_fields(masked, brace, object_end)
            line = module.source.line(brace)
            end_line = module.source.line(max(object_end - 1, brace))
            path: str | None = None
            dynamic = False
            if "path" in fields:
                value_start, _ = fields["path"]
                token = module.source.string_at(value_start)
                if token is not None and token.kind == "string":
                    path = token.value
                else:
                    dynamic = True
            is_index = "index" in fields and masked[fields["index"][0] : fields["index"][1]].strip() == "true"
            component = None
            for key in ("element", "Component", "component"):
                if key in fields:
                    value = masked[fields[key][0] : fields[key][1]]
                    found = re.search(r"<\s*([A-Z][\w$.]*)|^\s*([A-Z][\w$.]*)\s*$", value)
                    if found:
                        component = (found.group(1) or found.group(2)).split(".")[0]
                    break
            if dynamic:
                span = context.span_evidence(module.file.path, line, end_line)
                result.evidence.append(span)
                result.limitations.append(
                    context.limitation(
                        LIMITATION_DYNAMIC_ROUTING,
                        f"{module.file.path} line {line}: route object path is not a string literal, so the route was not extracted.",
                        [span["id"]],
                    )
                )
                full_path = None
            elif path is None and not is_index:
                full_path = parent or None
            else:
                full_path = _join_route(parent, path or "")
            if full_path is not None and (path is not None or is_index):
                self._route_node(context, module, modules, result, full_path, path, component, "react-router", line, end_line, is_index)
            if "children" in fields and full_path is not None:
                child_start = masked.find("[", fields["children"][0], fields["children"][1])
                if child_start != -1:
                    child_end = skip_balanced(masked, child_start, "[", "]")
                    self._object_route_array(context, module, modules, result, child_start, child_end, full_path)

    def _next_routes(self, context: RepositoryContext, modules: dict[str, Module], result: AnalysisResult) -> None:
        for module in modules.values():
            package = module.package
            if package is None or "next" not in package.dependencies:
                continue
            relative = module.file.path[len(package.directory) + 1 :] if package.directory else module.file.path
            if relative.startswith("src/"):
                relative = relative[4:]
            stem = re.sub(r"\.[cm]?[jt]sx?$", "", relative)
            line, end_line = 1, max(1, module.source.masked.count("\n") + 1)
            if stem.startswith("pages/"):
                route = stem[len("pages/") :]
                last = route.rsplit("/", 1)[-1]
                if last.startswith("_"):
                    continue
                if route.endswith("/index") or route == "index":
                    route = route[: -len("index")].rstrip("/")
                if route == "api" or route.startswith("api/"):
                    self._endpoint_node(context, module, result, ["ANY"], "/" + route, "next-pages", line, end_line)
                    continue
                component = module.default_export if module.default_export in module.components else None
                self._route_node(context, module, modules, result, "/" + route, "/" + route, component, "next-pages", line, end_line, False)
            elif stem.startswith("app/"):
                segments = [segment for segment in stem[len("app/") :].split("/") if not (segment.startswith("(") and segment.endswith(")"))]
                if not segments:
                    continue
                leaf = segments[-1]
                route = "/" + "/".join(segments[:-1])
                if leaf == "page":
                    component = module.default_export if module.default_export in module.components else None
                    self._route_node(context, module, modules, result, route, route, component, "next-app", line, end_line, False)
                elif leaf == "route":
                    methods = sorted({match.group(1) for match in NEXT_ROUTE_HANDLER.finditer(module.source.masked)}) or ["ANY"]
                    self._endpoint_node(context, module, result, methods, route, "next-app", line, end_line)

    def _route_node(
        self,
        context: RepositoryContext,
        module: Module,
        modules: dict[str, Module],
        result: AnalysisResult,
        full_path: str,
        declared: str | None,
        component: str | None,
        router: str,
        line: int,
        end_line: int,
        is_index: bool,
    ) -> None:
        span = context.span_evidence(module.file.path, line, end_line)
        result.evidence.append(span)
        route_node = node_id("route", context.repository_id, module.file.path, router, full_path, component or "")
        result.nodes.append(
            {
                "id": route_node,
                "type": "route",
                "name": full_path,
                "attributes": {
                    "repository_id": context.repository_id,
                    "path": full_path,
                    "declared_path": declared,
                    "index": is_index,
                    "router": router,
                    "component": component,
                    "module": module.file.path,
                    "line": line,
                },
                "evidence_ids": [span["id"]],
            }
        )
        result.relationships.append(self._edge("contains", module.node_id, route_node, [span["id"]]))
        if component:
            target = self._resolve_component(component, module, modules)
            if target is not None:
                result.relationships.append(
                    self._edge("references", route_node, target.node_id, [span["id"]], {"kind": "route-element"})
                )
        rendered = f" rendering {component}" if component else ""
        result.claims.append(
            self._claim(f"{module.file.path} declares {router} route {full_path}{rendered}.", [span["id"]])
        )

    def _endpoint_node(
        self,
        context: RepositoryContext,
        module: Module,
        result: AnalysisResult,
        methods: list[str],
        path: str,
        router: str,
        line: int,
        end_line: int,
    ) -> None:
        span = context.span_evidence(module.file.path, line, end_line)
        result.evidence.append(span)
        for method in methods:
            endpoint_node = node_id("endpoint", context.repository_id, module.file.path, router, method, path)
            result.nodes.append(
                {
                    "id": endpoint_node,
                    "type": "endpoint",
                    "name": f"{method} {path}",
                    "attributes": {
                        "repository_id": context.repository_id,
                        "http_method": method,
                        "path": path,
                        "controller": module.file.path,
                        "handler": method if method != "ANY" else "default",
                        "router": router,
                    },
                    "evidence_ids": [span["id"]],
                }
            )
            result.relationships.append(self._edge("exposes", module.node_id, endpoint_node, [span["id"]], {"router": router}))
            result.claims.append(self._claim(f"{module.file.path} exposes {router} API route {method} {path}.", [span["id"]]))

    # ----------------------------------------------------------------- requests
    @staticmethod
    def _own_instances(module: Module) -> dict[str, tuple[str, str | None]]:
        """``const api = axios.create({ baseURL })`` style client instances declared in a module."""

        masked = module.source.masked
        instances: dict[str, tuple[str, str | None]] = {}
        for match in INSTANCE_CREATE.finditer(masked):
            client = "axios" if match.group(2).startswith("axios") else "ky"
            args_end = skip_balanced(masked, match.end() - 1, "(", ")")
            base_url = None
            brace = masked.find("{", match.end(), args_end)
            if brace != -1:
                fields = object_fields(masked, brace, skip_balanced(masked, brace, "{", "}"))
                for key in ("baseURL", "prefixUrl"):
                    if key in fields:
                        token = module.source.string_at(fields[key][0])
                        if token is not None and token.kind == "string":
                            base_url = token.value
            instances[match.group(1)] = (client, base_url)
        return instances

    def _requests(self, context: RepositoryContext, modules: dict[str, Module], result: AnalysisResult) -> None:
        own_instances = {path: self._own_instances(module) for path, module in modules.items()}
        for module in modules.values():
            masked = module.source.masked
            instances = dict(own_instances[module.file.path])
            # Instances imported from another module in this repository, by their local names.
            for decl in module.imports:
                if decl.kind != "static":
                    continue
                target_path = module.resolved_imports.get(decl.specifier)
                if target_path is None or target_path not in own_instances:
                    continue
                exported = own_instances[target_path]
                for local, imported in decl.named.items():
                    if imported in exported and imported in modules[target_path].named_exports:
                        instances[local] = exported[imported]
                default = modules[target_path].default_export
                if decl.default_name and default in exported:
                    instances[decl.default_name] = exported[default]
            sites: dict[tuple[str, str, str], dict[str, Any]] = {}
            calls: list[tuple[int, str, str | None, str | None]] = []  # (paren offset, client, verb, instance)
            for match in FETCH_CALL.finditer(masked):
                calls.append((match.end() - 1, "fetch", None, None))
            for match in AXIOS_CALL.finditer(masked):
                calls.append((match.end() - 1, "axios", None, None))
            verb_pattern = re.compile(
                r"(?<![\w$.])(%s)\s*\.\s*(%s)\s*\("
                % ("|".join(["axios", "ky", *sorted(re.escape(name) for name in instances)]), "|".join(HTTP_VERBS))
            )
            for match in verb_pattern.finditer(masked):
                name = match.group(1)
                client = instances[name][0] if name in instances else name
                calls.append((match.end() - 1, client, match.group(2).upper(), name if name in instances else None))
            for paren, client, verb, instance in sorted(calls):
                args_end = skip_balanced(masked, paren, "(", ")")
                items = split_top_level(masked, paren + 1, args_end - 1)
                line = module.source.line(paren)
                end_line = module.source.line(max(args_end - 1, paren))
                span = context.span_evidence(module.file.path, line, end_line)
                if not items:
                    continue
                first_start, first_end = items[0]
                first = masked[first_start:first_end]
                method = verb or "GET"
                url: str | None = None
                url_kind = "literal"
                token = module.source.string_at(first_start)
                if token is not None:
                    url = token.value if token.kind == "string" else token.pattern
                    url_kind = "literal" if token.kind == "string" else "template"
                    if token.kind == "template" and not token.expressions:
                        url_kind = "literal"
                elif first.strip().startswith("{") and client == "axios" and verb is None:
                    brace = first_start + first.index("{")
                    fields = object_fields(masked, brace, skip_balanced(masked, brace, "{", "}"))
                    url_token = module.source.string_at(fields["url"][0]) if "url" in fields else None
                    if url_token is not None:
                        url = url_token.value if url_token.kind == "string" else url_token.pattern
                        url_kind = "literal" if url_token.kind == "string" or not url_token.expressions else "template"
                    method_token = module.source.string_at(fields["method"][0]) if "method" in fields else None
                    if method_token is not None and method_token.kind == "string":
                        method = method_token.value.upper()
                if url is None:
                    result.evidence.append(span)
                    result.limitations.append(
                        context.limitation(
                            LIMITATION_DYNAMIC_ROUTING,
                            f"{module.file.path} line {line}: {client} request URL is not a string or template literal, so the request target was not extracted.",
                            [span["id"]],
                        )
                    )
                    continue
                if client == "fetch" and len(items) > 1:
                    second_start, second_end = items[1]
                    brace = masked.find("{", second_start, second_end)
                    if brace != -1:
                        fields = object_fields(masked, brace, skip_balanced(masked, brace, "{", "}"))
                        if "method" in fields:
                            method_token = module.source.string_at(fields["method"][0])
                            if method_token is not None and method_token.kind == "string":
                                method = method_token.value.upper()
                            else:
                                result.evidence.append(span)
                                result.limitations.append(
                                    context.limitation(
                                        LIMITATION_DYNAMIC_ROUTING,
                                        f"{module.file.path} line {line}: fetch method is not a string literal; the request is recorded with method ANY.",
                                        [span["id"]],
                                    )
                                )
                                method = "ANY"
                base_url = instances[instance][1] if instance else None
                key = (method, url, client)
                result.evidence.append(span)
                site = sites.setdefault(
                    key,
                    {
                        "id": node_id("http_request", context.repository_id, module.file.path, client, method, url),
                        "type": "http_request",
                        "name": f"{method} {url}",
                        "attributes": {
                            "repository_id": context.repository_id,
                            "http_method": method,
                            "url": url,
                            "url_kind": url_kind,
                            "client": client,
                            "instance": instance,
                            "base_url": base_url,
                            "module": module.file.path,
                            "lines": [],
                        },
                        "evidence_ids": [],
                    },
                )
                site["attributes"]["lines"].append(line)
                site["evidence_ids"].append(span["id"])
            for site in sites.values():
                site["attributes"]["lines"] = sorted(set(site["attributes"]["lines"]))
                site["evidence_ids"] = sorted(set(site["evidence_ids"]))
                result.nodes.append(site)
                result.relationships.append(
                    self._edge("requests", module.node_id, site["id"], site["evidence_ids"], {"client": site["attributes"]["client"]})
                )
                prefix = f" (base URL {site['attributes']['base_url']})" if site["attributes"]["base_url"] else ""
                result.claims.append(
                    self._claim(
                        f"{module.file.path} sends {site['name']} with {site['attributes']['client']}{prefix}.",
                        site["evidence_ids"],
                    )
                )

    # -------------------------------------------------------------- limitations
    def _scan_limitations(self, context: RepositoryContext, modules: dict[str, Module], result: AnalysisResult) -> None:
        for module in modules.values():
            masked = module.source.masked
            for match in EVAL_CALL.finditer(masked):
                line = module.source.line(match.start())
                span = context.span_evidence(module.file.path, line)
                result.evidence.append(span)
                result.limitations.append(
                    context.limitation(
                        LIMITATION_REFLECTION,
                        f"{module.file.path} line {line} evaluates code at runtime (eval or new Function); its behavior is not analyzed.",
                        [span["id"]],
                    )
                )


# ------------------------------------------------------------------ utilities
def _package_name(specifier: str) -> str:
    parts = specifier.split("/")
    if specifier.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def _resolve_relative(from_path: str, specifier: str, files: set[str]) -> str | None:
    base = from_path.rsplit("/", 1)[0] if "/" in from_path else ""
    if specifier.startswith("/"):
        joined = specifier.lstrip("/")
    else:
        segments = base.split("/") if base else []
        for part in specifier.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if segments:
                    segments.pop()
                continue
            segments.append(part)
        joined = "/".join(segments)
    candidates = [joined, *[joined + suffix for suffix in RESOLVE_SUFFIXES], *[joined + "/index" + suffix for suffix in RESOLVE_SUFFIXES]]
    stem = re.sub(r"\.[cm]?js$", "", joined)
    if stem != joined:
        candidates.extend([stem + ".ts", stem + ".tsx"])
    for candidate in candidates:
        if candidate in files:
            return candidate
    return None


def _join_route(parent: str, path: str) -> str:
    if path.startswith("/"):
        return path if len(path) == 1 else path.rstrip("/")
    combined = "/".join(part for part in (parent.strip("/"), path.strip("/")) if part)
    return "/" + combined


def _tag_end(masked: str, i: int) -> int:
    depth = 0
    n = len(masked)
    while i < n:
        c = masked[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth == 0:
            return i + 1
        i += 1
    return n


def _jsx_attribute_string(source: JsSource, start: int, end: int, attribute: str) -> tuple[str | None, bool]:
    """Literal value of ``attribute="..."`` or ``attribute={"..."}``; (None, True) when dynamic."""

    match = re.search(rf"\b{attribute}\s*=\s*", source.masked[start:end])
    if not match:
        return None, False
    value_offset = start + match.end()
    token = source.string_at(value_offset)
    if token is not None and token.kind == "string":
        return token.value, False
    if source.masked[value_offset] == "{":
        inner_end = skip_balanced(source.masked, value_offset, "{", "}")
        token = source.string_at(value_offset + 1)
        if token is not None and token.kind == "string" and source.masked[token.end:inner_end - 1].strip() == "":
            return token.value, False
    return None, True


def _jsx_component_attribute(attributes_text: str) -> str | None:
    match = re.search(r"\belement\s*=\s*\{\s*<\s*([A-Z][\w$.]*)", attributes_text)
    if match:
        return match.group(1).split(".")[0]
    match = re.search(r"\b[cC]omponent\s*=\s*\{\s*([A-Z][\w$.]*)\s*\}", attributes_text)
    if match:
        return match.group(1).split(".")[0]
    return None
