# ADR-018: Cross-Repository HTTP Candidates and Bounded OpenAPI Inputs

- Status: Accepted
- Date: 2026-11-15

## Context

Issue #17 discovers frontend-to-backend candidates using the existing React
request and Spring Boot endpoint facts. D17 and ADR-003 require explicit
confirmation before a discovered cross-repository match becomes authoritative.
Issue #18 owns confirmation, rejection, stale confirmations, and required
confirmation CI failures. The user approved the bounded discovery scope below.

## Decision

### Discovery and authority

- A separate deterministic correlation stage runs after all source repository
  analyzers. It compares `http_request` nodes to `endpoint` nodes in other
  repositories. It never executes sources, requests a URL, or reads configuration
  values to discover a deployment.
- Every discovered link goes into the IR `candidates` array, never into
  `relationships`. Its status is `candidate`, its classification is `inferred`,
  and its confidence is less than 1. A unique high-scoring match is still a
  candidate. Observed declaration/containment/exposure relationships stay
  separate; the correlator does not create observed cross-repository links.
- Methods must agree exactly. Unknown `ANY` request methods are not matched.
  Paths match case-sensitively, segment by segment, with complete `{parameter}`
  segments acting as placeholders on either side. Parameter names can differ.
  Partial-segment interpolation, wildcards, regex patterns, runtime expressions,
  and optional or catch-all routes are not interpreted. Trailing slashes are
  significant. Query strings do not participate.
- Literal client base paths are concatenated with relative request paths;
  absolute request URLs override the base. Imported axios/ky instance declarations
  are included in request evidence. Runtime variables are not evaluated.
  A client whose base URL option is present but is not a supported literal
  (a runtime expression, a template, or an unsupported scheme) has an unknown
  base, not an absent one: it is marked `base_url_unresolved`, reported as a
  limitation, and its relative requests are excluded from matching. An absolute
  request URL on such a client still overrides the base and is matched.
- Endpoint server base paths come only from that endpoint's OpenAPI document.
  An OpenAPI server is not copied onto a similarly named Spring Boot service.
  A client URL alone establishes no backend ownership.
- If both request and endpoint have explicit origins, conflicting origins are
  excluded. Otherwise all matching backend candidates are retained. An unknown
  backend origin is not treated as a conflict or an origin match. Relative
  requests keep competing services, even when their declared origins differ.
- Confidence is a deterministic heuristic rank, not a probability:
  `0.65` for method/literal-path agreement, `0.55` for method/template-path
  agreement, and `0.9` when the matching endpoint also declares the same origin.
  There is no uniqueness bonus or automatic winner. Matching reasons are stored.

### Candidate identity and provenance

- Candidate identity is `semantic_id("candidate", "http", request_id, endpoint_id)`.
  Evidence IDs, hashes, revisions, confidence, and reasons are excluded.
  URL/path/method identities come from the underlying semantic source nodes.
- Each candidate stores the union of both nodes' evidence and the exact revision
  and revision kind of both participating repository snapshots. Source edits can
  change evidence and snapshot metadata without changing a candidate's semantic ID.
  Issue #18 can use this metadata to invalidate old confirmations.
- Candidate schema/runtime validation checks node types, different repositories,
  semantic identity, evidence on both sides, exact snapshot bindings, confidence,
  classification, and candidate-only state. It also re-derives the matches from
  the request and endpoint nodes and rejects stored candidates that differ, so a
  well-formed candidate with a path or origin its endpoint does not support fails.
- `scan` reports a JSON candidate count. Generated Markdown has an explicitly
  unconfirmed candidate section with reasons, confidence, revisions, and evidence.
  Candidates are not rendered as authoritative Mermaid architecture edges.

### OpenAPI subset

- Recognize JSON objects containing `openapi`, plus files named `openapi.*` or
  `swagger.*` so malformed/unsupported named inputs can produce limitations.
- Supported version strings are **3.0.x and 3.1.x**, with a numeric patch component.
  Swagger 2.0, OpenAPI 3.2.x, and YAML are reported as unsupported, not guessed.
- Extract `paths` and operations `get`, `put`, `post`, `delete`, `options`, `head`,
  `patch`, and `trace`. A service node represents each API document, not a verified
  deployed application. Endpoints are observed declarations with whole-file
  evidence, a schema path, method, and path.
- Server precedence is operation, then Path Item, then document. Supported servers
  are literal absolute HTTP(S) URLs and root-relative paths. Server variables are
  not substituted, even when they have defaults. Document-relative and
  protocol-relative servers are not interpreted. Unsupported servers produce
  limitations; their path/operation declarations can still be extracted.
- Follow only **same-document `#/...` JSON Pointers**. Path Item reference chains
  are bounded to 64 steps; cycles, non-object targets, unresolved pointers, and
  references with siblings are reported. Operation references are unsupported.
  Local payload-schema references are checked for resolution but not interpreted
  as endpoint/dependency facts; recursive payload schemas are not expanded.
- Inspect `$ref` values throughout the document. Remote URLs, file URLs,
  repository-relative files, paths outside the repository, unsupported anchors,
  and unresolved pointers are reported without opening any target. Even
  repository-local external files are not followed in this first subset.
- Payloads, descriptions, examples, security definitions, callbacks, and webhooks
  do not become graph facts. This is not a full OpenAPI validator or API client.

### URL safety

- A shared sanitizer runs before request node identity, attributes, names, or claims
  are created, and before OpenAPI server values are persisted.
- Remove userinfo, **all query strings**, and fragments. Removing all query values
  is safer than guessing which parameter names contain secrets. Default ports and
  hostname case are normalized; invalid ports, control characters, backslashes,
  dynamic hosts, and non-HTTP schemes are rejected.
- Candidate metadata stores only a sanitized path/method and an origin-supported
  flag, not credentials, query values, or source snippets. Error messages do not
  echo raw reference or server values. Original repository files are neither
  rewritten nor transmitted.

## Consequences

Discovery stays useful without pretending to know runtime routing or service
ownership. More competing candidates are preferable to silent false certainty.
The IR schema remains 1.0 while unreleased (ADR-014); old scans should be
regenerated for the new required `candidates` field. All confirmation/rejection
commands, stale-confirmation processing, and unresolved-confirmation exit code 5
remain outside #17.
