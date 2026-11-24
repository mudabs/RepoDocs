# ADR-035: Readable overview and focused diagram projections

- Date: 2026-11-23
- Status: Implemented for #74, submitted for review
- Decision: D51
- Related: ADR-026, ADR-028 and ADR-036 (HTTP jobs, #72)

## Decision

- Project one validated architecture view into deterministic Mermaid diagrams without further inference. Keep observed IR and model interpretation separate.
- Keep an overview. Groups of two or more components receive focused detail views. Oversized groups are sliced by deterministic breadth-first traversal of existing connections, at most ten focal components per detail. Disconnected members fill remaining capacity in deterministic order. These are presentation slices, not newly discovered architectural boundaries.
- Include at most eight directly connected boundary nodes per detail, ranked by connection count with stable identity/name tie-breaks. Report omitted boundary connections in comments. All original connections and citations remain in the evidence map; bounded details do not claim exhaustive visible coverage.
- Grouped overviews retain slice-member names for evidence navigation. Small views remain flat and are not padded with invented components or edges.
- Detail filenames contain a bounded readable repository/kind prefix and a hash of their exact identity. Slice suffixes are deterministic for identical input. Reject duplicate output names rather than silently overwriting a diagram. Changed grouping may change slice membership; regeneration safely retires obsolete owned files and refuses unexpected human edits.
- Keep inferred/proposed and boundary styling. Show a compact partial/grouped badge. Put detailed support, omitted-coverage and partial-reason diagnostics in Mermaid comments and preserve evidence IDs.
- Emit `architecture-view-index.md` with links to actual generated overview/detail filenames and the evidence map. The optional offline HTML export retains its diagram selector. Filenames in the index are generator-owned, never arbitrary model URLs.
- Emit ERD only from supported table facts. Competing interpretations remain in alternatives documentation. The current view schema has no deployment-role flag; a deployment entry supplied as an ordinary component can still appear and needs human review. Do not claim automatic deployment separation from filenames or kinds.

## Validation and limits

Use synthetic topology tests for chains, hubs, dense groups, disconnected nodes, identity collisions, reordered inputs and safe retirement. Also use small licensed source subsets from pinned DigitalBoneBox/Flaskr revisions with recorded content hashes, actual scanner/resolver evidence, scripted reference proposals and the full generation/validation path. These subsets are scanned as local content, not impersonated upstream commits. Renderer-required CI checks real Mermaid. Inspect representative rendered output for readability, but retain intended-user and real-model quality acceptance under #54/#77.

No new renderer, model calls, runtime ordering, deployment inference or semantic correctness guarantee is introduced.
