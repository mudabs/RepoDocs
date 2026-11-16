"""Spring Boot framework adapter.

Extracts, from source text only, the Spring Boot facts that documentation needs:
the application and its build manifest, stereotyped classes (controllers, services,
repositories, entities, configuration, components), request mappings with literal
paths, JPA entity structure and associations, constructor and field dependencies
between extracted classes, and the keys of application configuration files.

Everything that depends on runtime behaviour is reported as a limitation rather
than extracted: functional routes, paths built from constants or placeholders,
reflection, SpEL, profiles and conditional beans, Lombok-generated members, custom
JPA queries, inheritance mappings, and sources in languages other than Java.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from ..ir import claim_id, node_id, relationship_id
from . import (
    CLASSIFICATION_OBSERVED,
    LIMITATION_AMBIGUOUS_REFERENCE,
    LIMITATION_DYNAMIC_ROUTING,
    LIMITATION_GENERATED_CODE,
    LIMITATION_REFLECTION,
    LIMITATION_RUNTIME_CONFIGURATION,
    LIMITATION_UNREADABLE_SOURCE,
    LIMITATION_UNSUPPORTED_CONSTRUCT,
    LIMITATION_UNSUPPORTED_LANGUAGE,
    AnalysisResult,
    RepositoryContext,
)
from .config_files import read_properties_keys, read_yaml_keys
from .java import Annotation, JavaUnit, TypeDecl, annotation_values, generic_arguments, parse_java, strip_generics


FRAMEWORK_NAME = "Spring Boot"
SPRING_BOOT_GROUP = "org.springframework.boot"

STEREOTYPE_ANNOTATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("application", ("SpringBootApplication",)),
    ("controller", ("RestController", "Controller")),
    ("service", ("Service",)),
    ("repository", ("Repository",)),
    ("entity", ("Entity",)),
    ("configuration", ("Configuration", "ConfigurationProperties")),
    ("component", ("Component", "ControllerAdvice", "RestControllerAdvice")),
)
REPOSITORY_INTERFACES = {
    "Repository",
    "CrudRepository",
    "ListCrudRepository",
    "PagingAndSortingRepository",
    "ListPagingAndSortingRepository",
    "JpaRepository",
    "ReactiveCrudRepository",
    "ReactiveSortingRepository",
    "MongoRepository",
    "ElasticsearchRepository",
    "CassandraRepository",
}
MAPPING_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": None,
}
ASSOCIATION_ANNOTATIONS = ("ManyToOne", "OneToMany", "OneToOne", "ManyToMany")
COLLECTION_TYPES = {"List", "Set", "Collection", "Map", "Optional", "ObjectProvider", "Provider", "Iterable"}
LOMBOK_ANNOTATIONS = {
    "Data",
    "Value",
    "Getter",
    "Setter",
    "Builder",
    "RequiredArgsConstructor",
    "AllArgsConstructor",
    "NoArgsConstructor",
    "Slf4j",
    "EqualsAndHashCode",
    "ToString",
}
INHERITANCE_ANNOTATIONS = ("MappedSuperclass", "Inheritance", "Embeddable", "Embedded", "ElementCollection", "Convert")
CONDITIONAL_PREFIX = "ConditionalOn"

CONFIG_FILE = re.compile(r"(?:^|/)(?:application|bootstrap)(?:-(?P<profile>[A-Za-z0-9_]+))?\.(?P<ext>properties|ya?ml)$")
REFLECTION_PATTERN = re.compile(
    r"Class\.forName\s*\(|\.getDeclaredMethods?\s*\(|\.getMethods?\s*\(|\.getDeclaredConstructors?\s*\(|"
    r"\.newInstance\s*\(|\bjava\.lang\.reflect\b|\.getDeclaredField\s*\("
)
FUNCTIONAL_ROUTE_PATTERN = re.compile(r"\bRouterFunction\b|\bRouterFunctions\s*\.\s*route\b|\bRequestPredicates\b")
PLACEHOLDER_PATTERN = re.compile(r"\$\{([^}:]+)(?::[^}]*)?\}")
GRADLE_VERSION_PATTERN = re.compile(
    r"""org\.springframework\.boot['"]\s*\)?\s*version\s*\(?\s*['"]([^'"]+)['"]"""
)
GRADLE_ROOT_NAME_PATTERN = re.compile(r"""rootProject\.name\s*=\s*['"]([^'"]+)['"]""")


@dataclass
class Detection:
    build_tool: str | None = None
    artifact_id: str | None = None
    version: str | None = None
    manifest_paths: list[str] = field(default_factory=list)
    application_units: list[tuple[JavaUnit, TypeDecl]] = field(default_factory=list)


@dataclass
class ExtractedType:
    unit: JavaUnit
    decl: TypeDecl
    stereotype: str
    node_id: str
    evidence_id: str

    @property
    def qualified(self) -> str:
        return self.unit.qualified(self.decl.name if self.decl.outer is None else f"{self.decl.outer}.{self.decl.name}")

    @property
    def package(self) -> str:
        return self.unit.package or ""


def _annotation_end(annotation: Annotation) -> int:
    """The last line of an annotation, which spans several lines when its arguments do."""

    return annotation.line + annotation.args.count("\n")


class SpringBootAnalyzer:
    name = "spring-boot"
    version = "1.0"

    # ----------------------------------------------------------------- detection
    def detect(self, context: RepositoryContext) -> bool:
        return self._detect(context, parse_sources=False) is not None

    def _detect(self, context: RepositoryContext, *, parse_sources: bool) -> Detection | None:
        detection = Detection()
        for source in context.files_named("pom.xml"):
            text = context.text(source.path)
            if text is None:
                continue
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                continue
            groups = {(element.text or "").strip() for element in root.iter() if element.tag.endswith("groupId")}
            if SPRING_BOOT_GROUP not in groups:
                continue
            detection.manifest_paths.append(source.path)
            detection.build_tool = detection.build_tool or "maven"
            artifact = root.findtext("{*}artifactId")
            detection.artifact_id = detection.artifact_id or (artifact.strip() if artifact else None)
            parent = root.find("{*}parent")
            if parent is not None and (parent.findtext("{*}groupId") or "").strip() == SPRING_BOOT_GROUP:
                version = (parent.findtext("{*}version") or "").strip()
                if version and "${" not in version:
                    detection.version = detection.version or version
            if detection.version is None:
                for dependency in root.iter():
                    if not dependency.tag.endswith("dependency"):
                        continue
                    if (dependency.findtext("{*}groupId") or "").strip() == SPRING_BOOT_GROUP:
                        version = (dependency.findtext("{*}version") or "").strip()
                        if version and "${" not in version:
                            detection.version = version
                            break
        for source in context.files_named("build.gradle", "build.gradle.kts"):
            text = context.text(source.path)
            if text is None or SPRING_BOOT_GROUP not in text:
                continue
            detection.manifest_paths.append(source.path)
            detection.build_tool = detection.build_tool or "gradle"
            match = GRADLE_VERSION_PATTERN.search(text)
            if match and detection.version is None:
                detection.version = match.group(1)
        if detection.build_tool == "gradle" and detection.artifact_id is None:
            for source in context.files_named("settings.gradle", "settings.gradle.kts"):
                text = context.text(source.path)
                match = GRADLE_ROOT_NAME_PATTERN.search(text or "")
                if match:
                    detection.artifact_id = match.group(1)
                    break
        if parse_sources or not detection.manifest_paths:
            for source in self._java_sources(context):
                text = context.text(source.path)
                if text is None or "@SpringBootApplication" not in text:
                    continue
                unit = parse_java(source.path, text)
                for decl in unit.types:
                    if decl.has_annotation("SpringBootApplication"):
                        detection.application_units.append((unit, decl))
                if not parse_sources and detection.application_units:
                    break
        if not detection.manifest_paths and not detection.application_units:
            return None
        detection.manifest_paths = sorted(set(detection.manifest_paths))
        return detection

    @staticmethod
    def _java_sources(context: RepositoryContext):
        for source in context.files_with_suffix(".java"):
            segments = source.path.split("/")
            if "src/test/" in source.path or segments[0] == "test" or "/test/" in f"/{source.path}":
                continue
            yield source

    # ------------------------------------------------------------------ analysis
    def analyze(self, context: RepositoryContext) -> AnalysisResult:
        result = AnalysisResult()
        detection = self._detect(context, parse_sources=True)
        if detection is None:
            return result
        repository = context.repository_id
        repo_node = context.repository_node_id

        manifest_evidence = [context.file_evidence(path) for path in detection.manifest_paths]
        result.evidence.extend(manifest_evidence)
        framework_node = node_id("framework", repository, "spring-boot")
        framework_evidence_ids = [item["id"] for item in manifest_evidence]
        result.nodes.append(
            {
                "id": framework_node,
                "type": "framework",
                "name": FRAMEWORK_NAME,
                "attributes": {
                    "repository_id": repository,
                    "version": detection.version,
                    "build_tool": detection.build_tool,
                    "manifests": detection.manifest_paths,
                },
                "evidence_ids": framework_evidence_ids,
            }
        )
        result.relationships.append(
            self._edge("depends_on", repo_node, framework_node, framework_evidence_ids, {"framework": FRAMEWORK_NAME})
        )
        version_text = f" {detection.version}" if detection.version else ""
        if detection.manifest_paths:
            text = f"Repository {repository} declares Spring Boot{version_text} in {', '.join(detection.manifest_paths)}."
            result.claims.append(self._claim(text, framework_evidence_ids))

        # Parse every Java source outside test trees once.
        units: list[JavaUnit] = []
        for source in self._java_sources(context):
            text = context.text(source.path)
            if text is None:
                evidence = context.file_evidence(source.path)
                result.evidence.append(evidence)
                result.limitations.append(
                    context.limitation(
                        LIMITATION_UNREADABLE_SOURCE,
                        f"{source.path} could not be decoded as UTF-8 or changed during the scan; it was not analyzed.",
                        [evidence["id"]],
                    )
                )
                continue
            units.append(parse_java(source.path, text))
        self._report_other_languages(context, result)

        extracted = self._extract_types(context, units, result)
        by_simple_name: dict[str, list[ExtractedType]] = {}
        for item in extracted:
            by_simple_name.setdefault(item.decl.name, []).append(item)

        services = self._services(context, detection, extracted, result, repo_node)
        self._attach_to_services(context, extracted, services, result, repo_node)
        self._endpoints(context, extracted, result)
        self._dependencies(context, extracted, by_simple_name, result)
        self._repositories(context, extracted, by_simple_name, result)
        self._entities(context, extracted, by_simple_name, result)
        self._configuration_files(context, services, result, repo_node)
        for unit in units:
            self._scan_limitations(context, unit, result)
        return result

    # ------------------------------------------------------------ helpers: IR
    @staticmethod
    def _edge(
        edge_type: str, source: str, target: str, evidence_ids: list[str], attributes: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        edge = {
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
    def _report_other_languages(self, context: RepositoryContext, result: AnalysisResult) -> None:
        for suffix, language in ((".kt", "Kotlin"), (".kts", "Kotlin script"), (".groovy", "Groovy"), (".scala", "Scala")):
            files = [item for item in context.files_with_suffix(suffix) if not item.path.endswith("build.gradle.kts")]
            files = [item for item in files if not item.path.endswith("settings.gradle.kts")]
            if not files:
                continue
            evidence = [context.file_evidence(item.path) for item in files]
            result.evidence.extend(evidence)
            result.limitations.append(
                context.limitation(
                    LIMITATION_UNSUPPORTED_LANGUAGE,
                    f"{len(files)} {language} source file(s) are not analyzed by the Spring Boot adapter, "
                    "which reads Java sources only.",
                    [item["id"] for item in evidence],
                )
            )

    def _extract_types(
        self, context: RepositoryContext, units: list[JavaUnit], result: AnalysisResult
    ) -> list[ExtractedType]:
        extracted: list[ExtractedType] = []
        for unit in units:
            for decl in unit.types:
                stereotype = self._stereotype(decl)
                if stereotype is None:
                    continue
                qualified = unit.qualified(decl.name if decl.outer is None else f"{decl.outer}.{decl.name}")
                evidence = context.span_evidence(unit.path, decl.first_line, decl.line, symbol=qualified)
                result.evidence.append(evidence)
                class_node = node_id("class", context.repository_id, qualified)
                item = ExtractedType(unit, decl, stereotype, class_node, evidence["id"])
                extracted.append(item)
                attributes: dict[str, Any] = {
                    "repository_id": context.repository_id,
                    "qualified_name": qualified,
                    "package": unit.package,
                    "path": unit.path,
                    "line": decl.line,
                    "kind": decl.kind,
                    "stereotype": stereotype,
                    "annotations": sorted({annotation.simple_name for annotation in decl.annotations}),
                }
                references = self._property_references(decl)
                if references:
                    attributes["property_references"] = references
                result.nodes.append(
                    {
                        "id": class_node,
                        "type": "class",
                        "name": decl.name,
                        "attributes": attributes,
                        "evidence_ids": [evidence["id"]],
                    }
                )
                result.claims.append(
                    self._claim(
                        f"{qualified} is a Spring {stereotype} in repository {context.repository_id}.", [evidence["id"]]
                    )
                )
        return extracted

    @staticmethod
    def _stereotype(decl: TypeDecl) -> str | None:
        for stereotype, names in STEREOTYPE_ANNOTATIONS:
            if decl.has_annotation(*names):
                return stereotype
        if decl.kind == "interface" and any(strip_generics(parent) in REPOSITORY_INTERFACES for parent in decl.extends):
            return "repository"
        return None

    @staticmethod
    def _property_references(decl: TypeDecl) -> list[str]:
        references: set[str] = set()
        annotations = list(decl.annotations)
        for member in decl.fields:
            annotations.extend(member.annotations)
        for method in decl.methods:
            annotations.extend(method.annotations)
            for parameter in method.parameters:
                annotations.extend(parameter.annotations)
        for annotation in annotations:
            if annotation.simple_name == "Value":
                references.update(match.group(1).strip() for match in PLACEHOLDER_PATTERN.finditer(annotation.args))
        return sorted(references)

    def _services(
        self,
        context: RepositoryContext,
        detection: Detection,
        extracted: list[ExtractedType],
        result: AnalysisResult,
        repo_node: str,
    ) -> list[tuple[str, str]]:
        """Create one service node per @SpringBootApplication class; returns (node id, base package)."""

        applications = [item for item in extracted if item.stereotype == "application"]
        services: list[tuple[str, str]] = []
        for item in applications:
            service_node = node_id("service", context.repository_id, item.qualified)
            name = detection.artifact_id if detection.artifact_id and len(applications) == 1 else item.decl.name
            evidence_ids = [item.evidence_id]
            result.nodes.append(
                {
                    "id": service_node,
                    "type": "service",
                    "name": name,
                    "attributes": {
                        "repository_id": context.repository_id,
                        "framework": FRAMEWORK_NAME,
                        "framework_version": detection.version,
                        "build_tool": detection.build_tool,
                        "artifact_id": detection.artifact_id,
                        "application_class": item.qualified,
                        "base_package": item.package,
                    },
                    "evidence_ids": evidence_ids,
                }
            )
            result.relationships.append(self._edge("contains", repo_node, service_node, evidence_ids))
            result.relationships.append(self._edge("contains", service_node, item.node_id, evidence_ids))
            result.claims.append(
                self._claim(
                    f"{item.qualified} starts Spring Boot service {name} in repository {context.repository_id}.",
                    evidence_ids,
                )
            )
            if item.decl.has_annotation("ComponentScan"):
                result.limitations.append(
                    context.limitation(
                        LIMITATION_RUNTIME_CONFIGURATION,
                        f"{item.qualified} customizes @ComponentScan; which classes Spring registers is not derived "
                        "from source.",
                        evidence_ids,
                    )
                )
            services.append((service_node, item.package))
        return services

    def _attach_to_services(
        self,
        context: RepositoryContext,
        extracted: list[ExtractedType],
        services: list[tuple[str, str]],
        result: AnalysisResult,
        repo_node: str,
    ) -> None:
        for item in extracted:
            if item.stereotype == "application":
                continue
            owner = None
            best = -1
            for service_node, base_package in services:
                if item.package == base_package or item.package.startswith(base_package + ".") or base_package == "":
                    if len(base_package) > best:
                        owner, best = service_node, len(base_package)
            if owner is None:
                result.relationships.append(self._edge("contains", repo_node, item.node_id, [item.evidence_id]))
                if services:
                    result.limitations.append(
                        context.limitation(
                            LIMITATION_RUNTIME_CONFIGURATION,
                            f"{item.qualified} lies outside the base package of every @SpringBootApplication class; "
                            "whether Spring registers it depends on component-scan configuration.",
                            [item.evidence_id],
                        )
                    )
            else:
                result.relationships.append(self._edge("contains", owner, item.node_id, [item.evidence_id]))
        if not services and extracted:
            result.limitations.append(
                context.limitation(
                    LIMITATION_RUNTIME_CONFIGURATION,
                    "No @SpringBootApplication class was found; extracted classes are attached to the repository "
                    "and the application boundary is unknown.",
                    sorted({item.evidence_id for item in extracted}),
                )
            )

    def _endpoints(self, context: RepositoryContext, extracted: list[ExtractedType], result: AnalysisResult) -> None:
        for item in extracted:
            if item.stereotype != "controller":
                continue
            class_paths, class_dynamic = self._mapping_paths(item.decl.annotation("RequestMapping"))
            for method in item.decl.methods:
                mapping = next((a for a in method.annotations if a.simple_name in MAPPING_ANNOTATIONS), None)
                if mapping is None:
                    continue
                symbol = f"{item.qualified}.{method.name}"
                evidence = context.span_evidence(item.unit.path, method.first_line, method.line, symbol=symbol)
                result.evidence.append(evidence)
                method_paths, method_dynamic = self._mapping_paths(mapping)
                if class_dynamic or method_dynamic:
                    source = "class-level" if class_dynamic else "method-level"
                    result.limitations.append(
                        context.limitation(
                            LIMITATION_DYNAMIC_ROUTING,
                            f"{symbol}: the {source} request mapping path is not a string literal, so the route "
                            "was not extracted.",
                            [evidence["id"]],
                        )
                    )
                    continue
                http_methods = self._http_methods(mapping)
                for class_path in class_paths or [""]:
                    for method_path in method_paths or [""]:
                        path = self._join_path(class_path, method_path)
                        if PLACEHOLDER_PATTERN.search(path):
                            result.limitations.append(
                                context.limitation(
                                    LIMITATION_RUNTIME_CONFIGURATION,
                                    f"{symbol}: route {path} contains a property placeholder resolved at runtime.",
                                    [evidence["id"]],
                                )
                            )
                        for http_method in http_methods:
                            endpoint_node = node_id("endpoint", context.repository_id, item.qualified, method.name, http_method, path)
                            name = f"{http_method} {path}"
                            attributes: dict[str, Any] = {
                                "repository_id": context.repository_id,
                                "http_method": http_method,
                                "path": path,
                                "controller": item.qualified,
                                "handler": method.name,
                            }
                            values = annotation_values(mapping.args)
                            for key in ("consumes", "produces"):
                                if key in values and values[key].is_literal:
                                    attributes[key] = sorted(values[key].literals)
                            result.nodes.append(
                                {
                                    "id": endpoint_node,
                                    "type": "endpoint",
                                    "name": name,
                                    "attributes": attributes,
                                    "evidence_ids": [evidence["id"]],
                                }
                            )
                            result.relationships.append(
                                self._edge("exposes", item.node_id, endpoint_node, [evidence["id"]], {"handler": method.name})
                            )
                            result.claims.append(
                                self._claim(f"{item.qualified} exposes {name} through handler {method.name}.", [evidence["id"]])
                            )

    @staticmethod
    def _mapping_paths(annotation) -> tuple[list[str], bool]:
        """Literal paths of a mapping annotation and whether any path was not a literal."""

        if annotation is None:
            return [], False
        values = annotation_values(annotation.args)
        paths: list[str] = []
        dynamic = False
        for key in ("path", "value"):
            if key in values:
                if values[key].is_literal:
                    paths.extend(values[key].literals)
                else:
                    dynamic = True
        return sorted(set(paths)), dynamic

    @staticmethod
    def _http_methods(annotation) -> list[str]:
        fixed = MAPPING_ANNOTATIONS.get(annotation.simple_name)
        if fixed:
            return [fixed]
        values = annotation_values(annotation.args)
        if "method" not in values:
            return ["ANY"]
        raw = values["method"].raw.strip().strip("{}")
        methods = sorted({part.strip().rsplit(".", 1)[-1] for part in raw.split(",") if part.strip()})
        return methods or ["ANY"]

    @staticmethod
    def _join_path(class_path: str, method_path: str) -> str:
        joined = "/".join(part.strip("/") for part in (class_path, method_path) if part.strip("/"))
        return "/" + joined

    def _dependencies(
        self,
        context: RepositoryContext,
        extracted: list[ExtractedType],
        by_simple_name: dict[str, list[ExtractedType]],
        result: AnalysisResult,
    ) -> None:
        for item in extracted:
            if item.stereotype == "entity":
                continue
            candidates: list[tuple[str, str, str, int]] = []
            for member in item.decl.fields:
                if member.static:
                    continue
                candidates.append((self._injected_type(member.type), "field", member.name, member.line))
            for method in item.decl.methods:
                if not method.is_constructor:
                    continue
                for parameter in method.parameters:
                    candidates.append((self._injected_type(parameter.type), "constructor", parameter.name, method.line))
            for type_name, via, member_name, line in candidates:
                matches = by_simple_name.get(type_name, [])
                if not matches:
                    continue
                symbol = f"{item.qualified}.{member_name}"
                evidence = context.span_evidence(item.unit.path, line, symbol=symbol)
                if len(matches) > 1:
                    result.evidence.append(evidence)
                    result.limitations.append(
                        context.limitation(
                            LIMITATION_AMBIGUOUS_REFERENCE,
                            f"{symbol} references {type_name}, which names {len(matches)} extracted classes; the "
                            "dependency was not resolved.",
                            [evidence["id"]],
                        )
                    )
                    continue
                target = matches[0]
                if target.node_id == item.node_id:
                    continue
                result.evidence.append(evidence)
                result.relationships.append(
                    self._edge(
                        "depends_on",
                        item.node_id,
                        target.node_id,
                        [evidence["id"]],
                        {"via": via, "member": member_name},
                    )
                )

    @staticmethod
    def _injected_type(type_name: str) -> str:
        simple = strip_generics(type_name)
        if simple in COLLECTION_TYPES:
            arguments = generic_arguments(type_name)
            if arguments:
                return strip_generics(arguments[-1])
        return simple

    def _repositories(
        self,
        context: RepositoryContext,
        extracted: list[ExtractedType],
        by_simple_name: dict[str, list[ExtractedType]],
        result: AnalysisResult,
    ) -> None:
        nodes = {node["id"]: node for node in result.nodes}
        for item in extracted:
            if item.stereotype != "repository":
                continue
            node = nodes[item.node_id]
            entity_name: str | None = None
            id_type: str | None = None
            for parent in item.decl.extends:
                if strip_generics(parent) in REPOSITORY_INTERFACES:
                    arguments = generic_arguments(parent)
                    if arguments:
                        entity_name = strip_generics(arguments[0])
                    if len(arguments) > 1:
                        id_type = arguments[1]
                    break
            node["attributes"]["entity"] = entity_name
            node["attributes"]["id_type"] = id_type
            node["attributes"]["query_methods"] = sorted(
                method.name for method in item.decl.methods if not method.is_constructor and method.annotation("Query") is None
            )
            custom = [method for method in item.decl.methods if method.annotation("Query") is not None]
            node["attributes"]["custom_query_methods"] = sorted(method.name for method in custom)
            for method in custom:
                symbol = f"{item.qualified}.{method.name}"
                evidence = context.span_evidence(item.unit.path, method.first_line, method.line, symbol=symbol)
                result.evidence.append(evidence)
                result.limitations.append(
                    context.limitation(
                        LIMITATION_UNSUPPORTED_CONSTRUCT,
                        f"{symbol} declares a custom @Query; the statement is not analyzed for table access.",
                        [evidence["id"]],
                    )
                )
            if entity_name:
                matches = [match for match in by_simple_name.get(entity_name, []) if match.stereotype == "entity"]
                if len(matches) == 1:
                    result.relationships.append(
                        self._edge(
                            "depends_on",
                            item.node_id,
                            matches[0].node_id,
                            [item.evidence_id],
                            {"via": "repository-entity"},
                        )
                    )
                    result.claims.append(
                        self._claim(f"{item.qualified} manages JPA entity {matches[0].qualified}.", [item.evidence_id])
                    )

    def _entities(
        self,
        context: RepositoryContext,
        extracted: list[ExtractedType],
        by_simple_name: dict[str, list[ExtractedType]],
        result: AnalysisResult,
    ) -> None:
        nodes = {node["id"]: node for node in result.nodes}
        for item in extracted:
            if item.stereotype != "entity":
                continue
            node = nodes[item.node_id]
            table = item.decl.annotation("Table")
            table_name: str | None = None
            if table is not None:
                values = annotation_values(table.args)
                name_value = values.get("name")
                if name_value is not None and name_value.is_literal and name_value.literals:
                    table_name = name_value.literals[0]
                elif name_value is not None:
                    result.limitations.append(
                        context.limitation(
                            LIMITATION_UNSUPPORTED_CONSTRUCT,
                            f"{item.qualified}: @Table name is not a string literal.",
                            [item.evidence_id],
                        )
                    )
            for annotation in item.decl.annotations:
                if annotation.simple_name in INHERITANCE_ANNOTATIONS:
                    result.limitations.append(
                        context.limitation(
                            LIMITATION_UNSUPPORTED_CONSTRUCT,
                            f"{item.qualified} uses @{annotation.simple_name}; JPA inheritance and embeddable "
                            "mappings are not modeled.",
                            [item.evidence_id],
                        )
                    )
            fields: list[dict[str, Any]] = []
            for member in item.decl.fields:
                if member.static or member.annotation("Transient") is not None:
                    continue
                entry: dict[str, Any] = {
                    "name": member.name,
                    "java_type": member.type,
                    "column": None,
                    "id": member.annotation("Id", "EmbeddedId") is not None,
                    "association": None,
                    "target": None,
                    "join_column": None,
                    "mapped_by": None,
                    "line": member.line,
                }
                column = member.annotation("Column", "JoinColumn")
                if column is not None:
                    values = annotation_values(column.args)
                    name_value = values.get("name")
                    if name_value is not None and name_value.is_literal and name_value.literals:
                        if column.simple_name == "Column":
                            entry["column"] = name_value.literals[0]
                        else:
                            entry["join_column"] = name_value.literals[0]
                association = member.annotation(*ASSOCIATION_ANNOTATIONS)
                if association is not None:
                    entry["association"] = association.simple_name
                    entry["target"] = self._injected_type(member.type)
                    values = annotation_values(association.args)
                    mapped_by = values.get("mappedBy")
                    if mapped_by is not None and mapped_by.is_literal and mapped_by.literals:
                        entry["mapped_by"] = mapped_by.literals[0]
                    symbol = f"{item.qualified}.{member.name}"
                    evidence = context.span_evidence(item.unit.path, member.line, symbol=symbol)
                    result.evidence.append(evidence)
                    targets = [match for match in by_simple_name.get(entry["target"], []) if match.stereotype == "entity"]
                    if len(targets) == 1:
                        result.relationships.append(
                            self._edge(
                                "references",
                                item.node_id,
                                targets[0].node_id,
                                [evidence["id"]],
                                {
                                    "association": association.simple_name,
                                    "field": member.name,
                                    "join_column": entry["join_column"],
                                    "mapped_by": entry["mapped_by"],
                                },
                            )
                        )
                        result.claims.append(
                            self._claim(
                                f"{item.qualified}.{member.name} is a {association.simple_name} association to "
                                f"{targets[0].qualified}.",
                                [evidence["id"]],
                            )
                        )
                for annotation in member.annotations:
                    if annotation.simple_name in INHERITANCE_ANNOTATIONS:
                        evidence = context.span_evidence(item.unit.path, member.line, symbol=f"{item.qualified}.{member.name}")
                        result.evidence.append(evidence)
                        result.limitations.append(
                            context.limitation(
                                LIMITATION_UNSUPPORTED_CONSTRUCT,
                                f"{item.qualified}.{member.name} uses @{annotation.simple_name}, which is not modeled.",
                                [evidence["id"]],
                            )
                        )
                fields.append(entry)
            node["attributes"]["table"] = table_name
            node["attributes"]["fields"] = fields
            node["attributes"]["id_fields"] = [entry["name"] for entry in fields if entry["id"]]
            if table_name:
                result.claims.append(
                    self._claim(f"{item.qualified} is mapped to table {table_name}.", [item.evidence_id])
                )

    def _configuration_files(
        self,
        context: RepositoryContext,
        services: list[tuple[str, str]],
        result: AnalysisResult,
        repo_node: str,
    ) -> None:
        for source in context.files:
            match = CONFIG_FILE.search(source.path)
            if match is None or "src/test/" in source.path:
                continue
            text = context.text(source.path)
            evidence = context.file_evidence(source.path)
            result.evidence.append(evidence)
            if text is None:
                result.limitations.append(
                    context.limitation(
                        LIMITATION_UNREADABLE_SOURCE,
                        f"{source.path} could not be decoded as UTF-8 or changed during the scan.",
                        [evidence["id"]],
                    )
                )
                continue
            extension = match.group("ext")
            if extension == "properties":
                keys = read_properties_keys(text)
                file_format = "properties"
            else:
                keys = read_yaml_keys(text)
                file_format = "yaml"
            configuration_node = node_id("configuration", context.repository_id, source.path)
            result.nodes.append(
                {
                    "id": configuration_node,
                    "type": "configuration",
                    "name": source.path,
                    "attributes": {
                        "repository_id": context.repository_id,
                        "format": file_format,
                        "profile": match.group("profile"),
                        "keys": keys.keys,
                        "values_recorded": False,
                    },
                    "evidence_ids": [evidence["id"]],
                }
            )
            owner = services[0][0] if len(services) == 1 else repo_node
            result.relationships.append(self._edge("contains", owner, configuration_node, [evidence["id"]]))
            result.claims.append(
                self._claim(
                    f"{source.path} configures {len(keys.keys)} property key(s) in repository {context.repository_id}; "
                    "values are not recorded.",
                    [evidence["id"]],
                )
            )
            for line, note in keys.notes:
                span = context.span_evidence(source.path, line)
                result.evidence.append(span)
                result.limitations.append(
                    context.limitation(
                        LIMITATION_UNSUPPORTED_CONSTRUCT,
                        f"{source.path} line {line}: {note}; keys inside it were not extracted.",
                        [span["id"]],
                    )
                )

    def _scan_limitations(self, context: RepositoryContext, unit: JavaUnit, result: AnalysisResult) -> None:
        """Report runtime-dependent constructs anywhere in a unit, including non-stereotyped classes."""

        def report(kind: str, line: int, message: str, symbol: str | None = None, line_end: int | None = None) -> None:
            evidence = context.span_evidence(unit.path, line, line_end, symbol=symbol)
            result.evidence.append(evidence)
            result.limitations.append(context.limitation(kind, message, [evidence["id"]]))

        for match in REFLECTION_PATTERN.finditer(unit.text):
            line = context.line_of(unit.path, match.start())
            report(LIMITATION_REFLECTION, line, f"{unit.path} line {line} uses reflection; dynamically loaded types and calls are not analyzed.")
        for match in FUNCTIONAL_ROUTE_PATTERN.finditer(unit.text):
            line = context.line_of(unit.path, match.start())
            line_text = unit.text.splitlines()[line - 1].strip()
            if line_text.startswith("import "):
                continue
            report(
                LIMITATION_DYNAMIC_ROUTING,
                line,
                f"{unit.path} line {line} declares functional routes (RouterFunction); their paths are not extracted.",
            )
            break
        for decl in unit.types:
            qualified = unit.qualified(decl.name if decl.outer is None else f"{decl.outer}.{decl.name}")
            lombok_annotations = [a for a in decl.annotations if a.simple_name in LOMBOK_ANNOTATIONS]
            lombok = sorted({a.simple_name for a in lombok_annotations})
            if lombok:
                report(
                    LIMITATION_GENERATED_CODE,
                    lombok_annotations[0].line,
                    f"{qualified} uses Lombok ({', '.join('@' + name for name in lombok)}); generated members are not visible to static extraction.",
                    qualified,
                    line_end=_annotation_end(lombok_annotations[0]),
                )
            generated = next((a for a in decl.annotations if a.simple_name == "Generated"), None)
            if generated is not None:
                report(
                    LIMITATION_GENERATED_CODE,
                    generated.line,
                    f"{qualified} is marked @Generated; generated code is reported, not analyzed.",
                    qualified,
                    line_end=_annotation_end(generated),
                )
            members = [(qualified, decl.annotations)]
            members.extend((f"{qualified}.{m.name}", m.annotations) for m in decl.methods)
            members.extend((f"{qualified}.{f.name}", f.annotations) for f in decl.fields)
            for symbol, annotations in members:
                for annotation in annotations:
                    simple = annotation.simple_name
                    if simple == "Profile" or simple.startswith(CONDITIONAL_PREFIX):
                        report(
                            LIMITATION_RUNTIME_CONFIGURATION,
                            annotation.line,
                            f"{symbol} is annotated @{simple}; whether it is active depends on runtime configuration.",
                            symbol,
                            line_end=_annotation_end(annotation),
                        )
                    elif simple == "Value" and "#{" in annotation.args:
                        report(
                            LIMITATION_RUNTIME_CONFIGURATION,
                            annotation.line,
                            f"{symbol} uses a SpEL expression in @Value; its value is evaluated at runtime.",
                            symbol,
                            line_end=_annotation_end(annotation),
                        )
