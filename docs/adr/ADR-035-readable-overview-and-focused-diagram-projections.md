# ADR-035: Readable overview and focused diagram projections

- Date: 2026-11-22
- Status: Accepted (implements #74, epic #63)
- References: ADR-007, ADR-021, ADR-022, ADR-026, ADR-028, ADR-030

## Context

Previous architecture diagram rendering exhibited two contrasting failure modes:
1. **Sparse diagrams hiding validated detail**: Views with twelve or fewer components (including multi-service applications such as DigitalBoneBox and Flaskr) generated only a single monolithic diagram. When views had more than twelve components, the renderer collapsed components into generic `kind (N)` summary boxes (e.g. `backend-service (4)`), hiding component names, responsibilities, and connections.
2. **Crowded, unreadable graphs**: As multi-service architectures scale, placing all components and inter-service connections into a single flat diagram produces visual clutter, unreadable edge crossings, and oversized legends that dominate the drawing canvas.

Furthermore, documentation lacked explicit distinction between static application structure, deployment alternatives, supported entity-relationship diagrams (ERDs), and unavailable diagram types (such as dynamic sequence or runtime activity traces).

## Decision

### 1. Deterministic multi-view projection from a single validated architecture view
- A single validated architecture view drives both system-level overview and subsystem-level detail projections.
- No additional inference or model calls are made to generate detail views; all projections are purely deterministic transformations of the validated graph.
- Small projects (where each subsystem has only one member and total components remain $\le 12$) remain simple with a single concise overview diagram (`architecture-view.mmd`), avoiding unnecessary node splits or artificial minimums.

### 2. Focused detail views and crowded overview decomposition
- When any repository within the view contains a subsystem kind with two or more member components (such as backend services or API modules), or when the total component count exceeds the overview limit (12), focused detail views are generated: `architecture-view-<repository-slug>-<kind-slug>.mmd`.
- In crowded overviews ($> 12$ components), components are decomposed by repository and subsystem kind, preserving high-level system structure.
- In focused detail views, non-focal components connected to focal components are projected as `stub` boundary nodes (rendered with dotted border styling `class <id> stub; stroke-dasharray: 3 3;`). Boundary context explains external interactions without pulling in unrelated internal components.
- Inbound and outbound connections across boundaries are preserved.

### 3. Compact legends and non-distorting diagnostics
- Diagram legends are kept compact and concise: `Inferred (solid)` for deterministic/inferred relationships, `Proposed / unverified (dashed)` for unverified model proposals, and `Boundary context (dotted)` when boundary stubs are present.
- Support diagnostics and discovery limitation notes are emitted as Mermaid comments (`%% note: ...`) rather than multiline visual text boxes, preventing layout distortion.

### 4. Discoverability, navigation, and generated-file ownership
- Detail diagram filenames are registered in `evidence_map.json` under the `diagrams` index, ensuring full discoverability across CLI, web, and export consumers without requiring a browser application.
- All projected diagram files follow the established generated-documentation ownership protocol (ADR-006, ADR-021): files carry generated headers, validate against content hashes, and safely retire obsolete detail files without overwriting or deleting human edits.

### 5. Explicit diagram catalog boundaries and explanations
- **Architecture diagrams**: Deterministically projected from validated IR or architecture view.
- **Entity-relationship diagrams (ERD)**: Generated (`erd.mmd`) only when supported PostgreSQL/relational schema table facts exist. When no schema facts are detected, documentation explicitly notes ERD absence rather than synthesizing schema.
- **Unavailable diagram types**: Static analysis cannot establish runtime invocation order or dynamic execution state without tracing or speculation. Sequence diagrams and runtime activity/state diagrams are explicitly documented as unavailable, avoiding fabricated runtime behavior.
- **Deployment separation**: Deployment options (Docker compose, Kubernetes, local processes) remain separate from static application component boundaries.

### 6. Grounding boundary and quality evaluation
- Clear projection and decomposition improve visual legibility and navigation, but do not prove semantic truth or improve underlying model factual accuracy.
- Factual grounding remains governed by ADR-028 (static fact matches and confirmed candidates).
- Human source review and the real-model quality gate (#54) remain open.

## Consequences

- Multi-service repositories automatically receive focused detail diagrams alongside the system overview.
- Small repositories stay clean and concise without extraneous diagrams.
- Cross-boundary connections remain understandable via stub nodes.
- Downstream tools and documentation exports discover all diagram artifacts through `evidence_map.json` and generated Markdown.
- Pinned Mermaid rendering is verified across all generated overview and detail projections.
