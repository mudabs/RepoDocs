# ADR-028: Valid citations are not supported claims

- Date: 2026-11-20
- Status: Accepted within the owner's local web-first scope (epic #63); recorded here for review.
- Issue: #65; epic #63. Builds on ADR-026 and ADR-027.
- Amends: the classification bullet of ADR-026 section 2 (see "Amendments").

## Context

A reviewed run of the architecture-discovery path produced a diagram whose citations all resolved, yet it assigned a UI component only backend files and labelled connections `GET /bones` and `POST /bones` while citing excerpts that held no such route. ADR-026 classification only says whether a claim cites its own member files. A resolving citation is a place to look, so a well-formed claim with real citation IDs could still read as an established fact.

## Alternatives

- **Reject every claim the checks cannot confirm.** Rejected: most stacks have no analyzer, so nearly all legitimate claims would be dropped and the view would be empty for exactly the applications it exists for.
- **Judge roles from file names or directories** (`server/` is backend, `static/` is frontend). Rejected: it is wrong for shared and server-rendered files, monorepos and unconventional layouts. No check here reads a name.
- **Ask a second model to verify the first.** Rejected: its verdict would be another unverified AI output, and it needs paid inference.
- **Add a `verified` value to classification.** Rejected: classification is about how a claim is tied to files, and a badge that automation cannot honestly give would mislead.

## Decision

Every component and connection in a view carries two separate, computed fields next to `classification` and `confidence`:

| Field | Meaning |
|---|---|
| `classification` | Unchanged: `inferred` when the citations include an excerpt of a member file of the claim (and, across repositories, a bound confirmed candidate); otherwise `proposed`. This is **member citation**. |
| `support` | New: `supported`, `unverified` or `unsupported`. This is whether a **deterministic fact** backs the claim. |
| `support_reasons` | New: stable reason codes (schema enum, with sentences in `SUPPORT_REASONS`). At least one. |

`support` and its reasons are computed by RepoDocs from the snapshot context and the System IR. The model cannot supply them, and `load_view` recomputes them, so a hand-edited badge makes a stored view `invalid`. The view never contains `observed`, and no value of `support` says a relationship is true or is correct.

What each value means:
- `supported`: at least one deterministic check passed. The checks are: an endpoint the analyzers observed in a member file; a route literal, with its method nearby, in a cited excerpt of a member file; observed analyzer facts of the claimed role in a member file; or a confirmed cross-repository candidate bound to the connection (ADR-026, unchanged). Each is a fact about text or analyzer output, not about runtime behavior.
- `unverified`: nothing checkable was found, or only half of a checkable claim was found. This is the normal state for a stack without an analyzer and for labels that are not literal routes. The claim stays visible, marked as unverified.
- `unsupported`: a checkable literal is contradicted or absent from every cited member excerpt, or the observed facts of the member files are only of a different role. The claim is kept in the stored view and the evidence map with its reasons, and left out of the default diagrams.

### Checks

Route labels. A connection label of the form `[METHOD ]/path` is a literal claim. Parameter segments (`:id`, `{id}`, `<int:id>`, `${id}`) match one segment. For `calls`, `reads` and `writes` the route must appear at both ends: the server end (an observed endpoint, or the literal in a cited member excerpt) and the call site (the literal in a cited member excerpt of the source). One end only gives `unverified` with `route-on-one-side-only`, for example an endpoint that exists but is never called by the cited client code. For other kinds either end suffices. A method is accepted when its word is within a small line window of the route literal (one line before, three after), and a claimed `GET` fails only when a write method is on the route's own line, since a bare `fetch` names no method. Only excerpts that are member files of the connection's components count, so a route in an unrelated excerpt supports nothing.

Component roles. Observed facts in the member files come from the IR (`component`, `route`, `http_request` for frontend; `endpoint` for server; `table` for data), never from names. A `frontend` is `unsupported` only when its members carry server or data facts and no frontend fact. A `backend-service` or `api` is `unsupported` only when its members carry frontend facts and no server or data fact. A file with both kinds of fact, such as shared or server-rendered code, is never refused. A role with no observed facts is `unverified`.

### Presentation

- Default Mermaid diagrams omit `unsupported` components and connections (and connections to omitted components), and say how many were left out. A line is drawn solid only when it is `inferred` and `supported`. Every other line or node is dashed, and `unverified` items carry the text mark `(unverified)` in addition to `(proposed)` and `(low confidence)`. No mark or legend says verified or correct.
- The evidence map holds `support` and `support_reasons` for every component and connection, including omitted ones. The HTML page lists every component and connection with its support level and shows the reason sentences. Alternatives are unchanged.
- Structurally rejected model output (unknown IDs, bad paths, unbound candidates) stays counted by reason in `rejected`, with no text stored, as in ADR-026. Unsupported claims are different: they are well formed, so they are stored.

### Schema and versioning

`schemas/architecture-view-1.0.json` gains the required `support` and `support_reasons` fields and keeps version `1.0`, because schemas stay `1.0` while unreleased (ADR-014). A view stored before this change lacks the fields and is reported `invalid` on load; run `repodocs architecture discover --refresh`. The classification enum, the prompt, the response schema, the context bundle, consent and the IR do not change.

## What automation can and cannot establish

Can: that a cited excerpt really contains a claimed route, that an analyzer observed an endpoint or a role fact in a member file, and that a confirmed human decision is bound to a connection.

Cannot: that a call is made at runtime, that a handler does what its label says, that components own the files assigned to them, that a connection's direction is right, or anything about code outside the cited excerpts. A route built from a prefix in another file is reported `unsupported` for its literal check even if it is real, and the reason says so. A `supported` claim can still be wrong, and an `unverified` or `unsupported` one can still be right. Human review of the source against the published diagram is still required and stays with the #54 quality gate; passing these checks does not satisfy it.

## Consequences

- A fabricated route or a mislabelled component no longer reaches the default diagram merely because its citation IDs resolve, and it stays inspectable with its reasons.
- On stacks with no analyzer, components are `unverified`. The default view is honest about that instead of looking authoritative.
- There are false negatives (real routes built across files) and false positives (a method word near an unrelated route). The checks are deliberately small, are tested in both directions, and do not claim completeness.
- Existing stored views must be refreshed once.

## Amendments

- **ADR-026 section 2 (classification).** Classification is unchanged and now means member citation only. `support` is the separate answer to whether a deterministic fact backs the claim. A model's label, confidence or valid citation never produces `supported`.
- **ADR-027 / `docs/WEB_WORKFLOW.md`.** The browser journey's inspection step shows `support` next to `classification`; claims with resolving citations are not automatically supported.
