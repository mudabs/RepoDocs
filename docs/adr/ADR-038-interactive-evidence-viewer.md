# ADR-038: Interactive evidence viewer and artifact downloads

- Date: 2026-11-24
- Status: Accepted
- Issue: #75; epic #63
- Implements: D43, D51, ADR-027, ADR-035

## Context

Issue #73 delivered the browser workflow shell and job lifecycle UI, leaving a placeholder for
the diagram viewer. Issue #75 adds interactive Mermaid diagram rendering, pan/zoom controls,
evidence navigation, revision badges, and artifact downloads.

## Decision

- Render `.mmd` artifacts in `DiagramViewer.tsx` using the pinned `mermaid` npm package
  with `startOnLoad: false` and `securityLevel: 'strict'`. Insert parsed SVG DOM nodes via
  `DOMParser` instead of `dangerouslySetInnerHTML`. Invalid diagrams render a plain-text
  error message.
- Provide pan (mouse drag) and zoom (mouse wheel and toolbar buttons) via CSS `transform`
  (`translate` and `scale`) on the SVG container.
- Switch between available `.mmd` artifacts in `ScopeSelector.tsx` grouped by view scope
  (`overview`, `components`, `erd`) matching the backend's `_view_of` mapping.
- Display evidence in `EvidencePanel.tsx` from optional JSON index/architecture-view artifacts
  and `%% evidence` comments embedded in the active Mermaid diagram. Source file references
  are filtered to allowlisted pinned GitHub blob URLs (`40`-hex SHA) or `<source:name>` labels;
  raw server filesystem paths are never displayed.
- Display source identity in `RevisionBadge.tsx`: pinned GitHub commit links only when owner,
  repository, and clean `40`-hex SHA are present; otherwise `"Local source (unverified)"` with
  the `source_ir_hash` fingerprint.
- Serve downloads via `<a href="/api/v1/jobs/{jobId}/artifacts/{artifactId}" download>` in
  `ArtifactDownloads.tsx` so the backend enforces ownership, manifest hash checks, and
  `Content-Disposition: attachment`.
- Display an explicit `"not current"` banner whenever `job.state !== 'succeeded'` and never
  render a `"verified architecture"` badge.

## Alternatives considered

- Generating diagrams client-side from raw IR: rejected because it would duplicate
  `repodocs.render` and `repodocs.web.views` logic in JavaScript (violating D02).
- Adding a separate `GET /api/v1/jobs/{id}/artifacts` listing endpoint: unnecessary because
  `job.result.artifact_ids` already provides the validated artifact list.

## Consequences

- Client-side Mermaid rendering uses the same `mermaid` ^11 package already pinned in
  `frontend/package.json`.
- Artifact downloads remain gated by session cookies and backend manifest verification.
