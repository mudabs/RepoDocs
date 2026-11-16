# ADR-015: Built-in Analyzer Contract, Span Evidence, and Reported Limitations

- Date: 2026-11-14
- Status: Accepted

## Context

ADR-005 settled that the first analyzers are trusted Python modules, and ADR-002 settled that evidence records repository revision, path, and symbol or line range where available. The Milestone 1 scanner produced only repository, language, manifest, and dependency facts with whole-file evidence, and the System IR had no place for constructs an analyzer saw but could not turn into facts. Issues #14, #15, and #16 (Spring Boot, React, and PostgreSQL analysis) all depend on a stable analyzer contract and all require that dynamic routing, reflection, generated code, and unsupported constructs are reported rather than inferred.

## Alternatives

- Grow the monolithic scanner with framework-specific branches and whole-file evidence only.
- Introduce a parser dependency (for example Tree-sitter) before any adapter exists, and design the contract around its syntax trees.
- Define a small in-process analyzer contract now, with line-level evidence and an explicit limitations list, and add parser dependencies later behind the same contract.
- Encode unsupported constructs as low-confidence claims instead of a separate list.

## Decision

### Analyzer contract
- A built-in analyzer is a Python object with `name`, `version`, `detect(context)`, and `analyze(context)` in `repodocs.analyzers`. `detect` is cheap and decides whether the analyzer applies to a repository; `analyze` runs only when it does. The scanner runs analyzers in name order after the basic manifest facts, and merges their contributions.
- Analyzers receive a read-only `RepositoryContext`: the discovered files, the snapshot revision and revision kind, and hash-checked access to file text. Text that fails the discovery-time hash or UTF-8 decoding is unavailable and must be reported as a limitation. Analyzers never execute repository code, build tools, or package managers.
- Analyzers contribute nodes, relationships, evidence, claims, and limitations. Every node, relationship, claim, and limitation they add carries evidence created through the context, so it resolves to the snapshot revision.
- `provenance.analyzers` lists every analyzer offered to the scan with its version and the repositories where it detected its framework and ran. Each evidence record names the analyzer that recorded it.

### Evidence and classification
- Evidence has kind `file` (the whole file) or `span` (a 1-based inclusive line range inside the file with an optional qualified symbol). Span evidence carries the hash of the whole file and the same revision as the file, and its identity includes the line range and symbol.
- A relationship or claim is `observed` with confidence 1.0 only when it is read literally from source, for example an annotation with a string-literal path, a field whose declared type is another extracted class, or a repository interface whose generic argument names an extracted entity. Anything matched by name across declarations, files, or repositories is `inferred`, never `observed`.
- Relationships may carry an optional `attributes` object for typed detail such as the member a dependency is injected through or a JPA association kind.

### Limitations
- The IR gains a required `limitations` list. Each entry names the repository, the analyzer, a kind from a closed set (`dynamic-routing`, `reflection`, `generated-code`, `runtime-configuration`, `unsupported-construct`, `unsupported-language`, `unreadable-source`, `ambiguous-reference`), a message, and evidence. Limitations are rendered in generated documentation and are never turned into nodes, relationships, or claims.
- Configuration analyzers record property keys only. Values are never read into the IR, so credentials and connection strings cannot reach documentation or model inputs.
- A limitation's evidence is the span of the construct that triggers it. For an annotation that is the annotation itself, from its first line to its last when its arguments span lines, not the member that carries it. The golden-fixture harness checks this (ADR-024).
- Sources under test trees (`src/test/`) are not analyzed for framework facts.

### Spring Boot adapter scope (issue #14)
The first adapter detects Spring Boot from Maven or Gradle manifests or an `@SpringBootApplication` class and extracts, from Java sources only: the application service, stereotyped classes (controllers, services, repositories, entities, configuration, components), request mappings with literal paths, constructor and field dependencies between extracted classes, Spring Data repository to entity links, JPA entity fields and associations, and configuration file keys. It reports functional routes, non-literal mapping paths, property placeholders in routes, reflection, SpEL, `@Profile` and `@ConditionalOn*`, Lombok and `@Generated`, custom `@Query` statements, JPA inheritance and embeddable mappings, Kotlin, Groovy, and Scala sources, ambiguous simple names, and classes outside every application base package.

## Consequences

The IR, CLI response, and manifest schemas remain at version 1.0 because nothing has been released (ADR-014); from the first tagged release onward these additions require a version bump. Java is scanned structurally by a small comment-masking declaration scanner rather than a full parser; it skips method bodies and initializers and leaves out what it cannot recover, so depth is bounded and measured by fixtures, not asserted. A parser dependency can be added behind the same contract later. The external subprocess analyzer protocol from ADR-005 is still unimplemented. Generated Markdown gains analysis coverage, services, endpoints, entities, and reported-limitation sections, so claims and limitations are visible to reviewers.
