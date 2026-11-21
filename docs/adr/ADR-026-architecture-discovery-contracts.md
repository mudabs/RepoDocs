# ADR-026: Architecture Discovery Contracts and Decision Gates

- Status: Accepted by the owner's instruction of 2026-11-17 (epic #47), recorded here for review. Items under "Awaiting owner" are not accepted.
- Date: 2026-11-17
- Amends: ADR-020 and ADR-021 (see "Amendments"); D35 and D36 in `docs/DECISIONS.md`
- Issue: #48 (epic #47)

## Context

The deterministic analyzers extract framework facts (Spring Boot, React, OpenAPI, PostgreSQL). For a repository outside those frameworks, such as a vanilla JavaScript and Express application, the System IR holds languages, manifests, and dependencies, and the diagram is a repository and language inventory. That does not explain what the application does or how its parts connect.

ADR-020 forbids source text in what a provider receives (D35), and ADR-021 forbids AI from changing any diagram (D36). Both were right for claims about analyzed facts. Neither can produce an architecture view. The owner has authorized, for architecture discovery only: bounded, redacted, read-only source excerpts and controlled retrieval of further files; and AI proposals that drive a separate, versioned, inferred view used for diagrams. This ADR defines those contracts so that implementation (#49 to #53) can follow them, and states what stays unchanged.

The owner's authorization does not permit sending repository content to an external service. External transmission needs recorded consent that names source excerpts.

## Alternatives

- **Let the model write Mermaid, HTML, or SVG.** Rejected: unrestricted markup carries injection and unreviewable content. The model proposes a structured model and RepoDocs renders it.
- **Add model output to the System IR.** Rejected, as in ADR-020: the IR hash and every confirmation would depend on a model.
- **Widen the evidence bundle (ADR-020) to include excerpts.** Rejected: the IR-only bundle and its consent are an established, reviewed contract. Excerpts go in a separate context bundle with a separate consent.
- **Send the whole repository.** Rejected: unbounded disclosure and cost.
- **More deterministic analyzers per stack.** Not rejected as a long-term option, but it does not scale to arbitrary stacks and is out of scope (epic #47).
- **Let the model call arbitrary tools.** Rejected: the model may only ask for files by path, and a resolver the engine owns decides.

## Decision

### 1. Separate artifacts, separate authority

| Artifact | Contents | Authority |
|---|---|---|
| System IR (`ir.json`) | Observed facts, evidence, candidates | Authoritative; unchanged by this epic |
| Evidence bundle (ADR-020) | IR facts only, no source text | Unchanged |
| Architecture context preview (`.repodocs/architecture-context.json`) | What `architecture context` would send, for review | Data for the reviewer; never published; never used by a stored view |
| Saved context (`.repodocs/architecture-view-context-<hash>.json`) | The exact redacted excerpts a stored view was built from, named by their hash | Data the view is revalidated against; never published; never overwritten |
| Architecture view (`.repodocs/architecture-view.json`) | Proposed components and connections with evidence | `proposed` or `inferred` only, never `observed` |
| Generated architecture documents | Deterministic rendering of a current, valid view | Derived; labeled `repodocs-architecture` in the manifest |

A view never edits the IR, a candidate, or a confirmation, and never makes a cross-repository link authoritative. `architecture.md` and `architecture.mmd` stay byte-identical for the same IR whether a view exists or not (ADR-021). The view produces separate files.

### 2. Architecture view contract (`schemas/architecture-view-1.0.json`, example in `docs/examples/`)

- Components carry a stable ID (`arch:` plus 16 hex digits of the hash of the repository ID and the normalized name, so a rename is a new component and a rerun is not), a closed `kind`, a one-sentence responsibility, member paths in one repository, at least one evidence ID, a classification, and a confidence.
- Connections carry a stable ID derived from source, target, and kind, a closed `kind` (`calls`, `reads`, `writes`, `serves`, `depends-on`, `publishes`, `renders`), a short label, evidence IDs, a classification, a confidence, and `cross_repository`. There is no ordering or time field, so a view cannot claim a runtime sequence.
- `alternatives` hold competing interpretations the model reported instead of choosing. They are shown, never dropped to make the diagram tidier.
- Evidence IDs (`ctx:` plus 16 hex digits) name excerpts in the context bundle that was sent, each of which resolves to a repository, path, content hash, and line span in the snapshot. A citation to anything else is rejected.
- Provenance records provider, requested and serving model, engine, prompt, and context versions, the context hash, the IR hash, every repository's revision and content fingerprint, whether excerpts left the machine, and the rounds and requests used. There are no timestamps, so identical inputs give identical files.
- `status` is `complete` or `partial` (a budget, timeout, or provider error stopped discovery; the reason is recorded). `stale` and `invalid` are computed when the view is loaded and are never stored.
- Classification: a component is `inferred` only when it cites at least one excerpt of one of its own member files. A connection is `inferred` only when its citations include an excerpt of a member file of its source and of a member file of its target, and, across repositories, only through a confirmed candidate that is bound to that connection (below). Evidence from one side only, or from files that are members of neither, leaves it `proposed`. Otherwise it is `proposed`. A connection with no valid citation is rejected. A model's own label never promotes it. Confidence is the model's statement and is not evidence.
- Cross-repository connections are always at most `proposed`, rendered as unconfirmed, unless they name a candidate that is `confirmed` and **bound to the connection**. A confirmed decision is about one observed request and one observed endpoint, so naming the same two repositories is not enough, and unrelated components in those repositories never inherit it. The binding comes from the IR itself: the candidate's request node must be in the connection's source repository and one of its evidence files must be a member file of the source component; the candidate's endpoint node must be in the target repository and one of its evidence files a member file of the target component; and the connection's citations must include an excerpt of a member file on each side. A reversed connection, a component pair that merely shares the two repositories, a candidate whose observed locations are not recorded, or evidence from one side only leaves the connection `proposed`, never `inferred` through that candidate, and is counted (`unbound-candidate`, or `unconfirmed-candidate` when the candidate is not confirmed). The same rules run again when a stored view is loaded, so a stored inference that the candidate does not support makes the view `invalid`. A model cannot confirm a candidate, and a view cannot satisfy the required-confirmation check (exit code 5).
- Structural validation (schema, IDs, paths exist in the snapshot, citations resolve, member and endpoint rules) shows that a view is well formed. It does not show that a relationship is true. Every document that renders a view says so.

### 3. Context bundle contract (`schemas/architecture-context-1.0.json`)

What may be sent:
- A bounded directory and file inventory (names only), manifests, README text, and selected line spans of text source files.

What may never be sent or read:
- Anything the existing scanner refuses: symlinks, directories that leave the repository, binary and oversized files, and files not in the snapshot.
- Files whose names say they hold secrets (`.env*`, `*.pem`, `*.key`, `*.p12`, `id_rsa*`, `*.keystore`, `credentials*`, `secrets.*`, `.npmrc`, `.netrc`, `.git*`), lockfiles, minified or generated files, and version control internals. This deny list is a floor; a project may add names and cannot remove them.
- A file whose content hash no longer matches the snapshot (stale).

Redaction runs on every excerpt before it is stored, previewed, or sent, with the rules version from ADR-020 (1.1 now). The bundle records counts, never the originals. Redaction is pattern-based and is not a completeness guarantee, so consent is still required and the bundle is stored for review before any provider is contacted.

Default budgets (the project may lower them, never raise them above these without a new policy version):

| Budget | Default |
|---|---|
| Rounds of exploration | 3 |
| Provider requests, including one repair retry | 6 |
| Files the model may request per round | 8 |
| Excerpt size | 8 KiB (200 lines) |
| File read for an excerpt | 64 KiB |
| Total context sent | 200 KiB (about 50,000 tokens) |
| Provider response | 32 KiB |
| Inventory | 500 paths per repository |

Exhausting a budget ends discovery with `partial` and the reason. It never raises a limit and never retries more than the budget allows. This is a bound on a single run. Broader quotas and spending controls are deferred to a separate change and are not implied here.

Retention: the context bundle lives in `.repodocs/`, which is local state and is not generated documentation or published. It holds redacted excerpts only. `repodocs` never writes original unredacted text outside the repository being analyzed. Deleting the file discards it.

Model file requests: the model may ask for `{repository, path, line range}`. The engine's resolver checks the allow and deny rules, the snapshot, hashes, and budgets, and answers with a redacted excerpt or an omission reason. The model cannot run commands, import code, follow links, or reach the network. Repository text, including instructions inside a README or source, is data. The prompt says so, and validation does not depend on the model obeying.

### 4. Consent and local-only behavior

- Consent for source excerpts is a separate setting from `external_consent`: `ai.source_excerpts_consent`, which records the context policy version it was given for. Consent given for the IR-only bundle never authorizes excerpts. Changing the policy version invalidates the recorded consent.
- An external provider (Anthropic, or Ollama on a non-loopback host) needs both consents. Without the second, discovery stops with exit code 2 before the context is built for sending, and nothing leaves the machine.
- A loopback Ollama host sends nothing off the machine. It needs no consent, but discovery still runs only when the user runs the discovery command, and redaction and budgets still apply.
- With provider `none`, discovery is unavailable and says so.
- Neither provider follows redirects (ADR-025), and credentials come from the environment.

### 5. Commands, configuration, and persisted state (implemented in #53)

- `repodocs architecture context` builds and writes the context bundle and prints a summary. It contacts nothing. It is the review step before consent.
- `repodocs architecture discover [--refresh]` runs the bounded exploration with the configured provider and writes the view. Without `--refresh`, a current view is reused and no provider is contacted. The context preview (`architecture context`) is a separate file, so previewing again never touches a stored view or the context behind it, and cannot invalidate an existing multi-round discovery. A provider failure, an unusable reply, or a write failure during `--refresh` leaves the previous view and its own context in place and usable, and later `generate` and `validate` keep working from them (a write failure is reported as such, exit 2, with the previous view unchanged). A refresh that ends early (`status: partial`) does not displace a current, complete view; the response says the replacement was discarded and why.
- `repodocs generate` renders architecture documents from a current stored view and never calls a provider. A stale or invalid stored view produces a warning, retires its owned, unedited files, and leaves the deterministic documents only.
- **A clean clone has no stored view, and that alone never deletes published files.** The view is local state: it depends on source excerpts and is not committed. When no stored view exists at all, `generate` keeps the existing `architecture-view*` files after the usual check that they are owned and unedited, and keeps each one's manifest entry exactly as recorded, so a kept file is never labelled as generated from the current System IR. `generate` and `validate` report that their freshness was not verified, and the publication pull request lists them as kept and unverified. `validate` accepts a kept file when its hash matches the manifest and it passes the structural checks; it does not claim it is current. Edited files still stop both commands (exit code 4). A known-stale or invalid stored view still retires its files. To remove them on purpose, without a prompt, run `repodocs generate --retire-architecture` (owned, unedited files only); delete the stored view files as well if they should not return on the next run. Source excerpt bundles and secrets are never committed to make this work.
- `repodocs ai-configure` gains `--consent-source-excerpts` and `--revoke-source-consent`. The project config gains `ai.source_excerpts_consent` and an optional `architecture` block holding lower budgets and extra deny names.
- Exit codes are the existing ones: 2 for configuration or missing consent, 5 for unresolved confirmations, 7 for provider failure. No new code is added.
- All responses use the existing versioned JSON envelope.

### 6. Failure semantics

| Situation | Result |
|---|---|
| No provider, or `none` | `discover` exits 2 with the reason; `generate` is unaffected |
| External provider without source consent | Exit 2, nothing built for sending, nothing sent |
| Provider error, refusal, timeout | Exit 7; a partial view is stored only when it has at least one valid component, with `partial_reason` and a visible banner in rendered output |
| Completion | A view is `complete` only when the model's last reply is valid, says `complete: true`, and holds a component. Otherwise it is `partial` with a reason: `incomplete-response` (no continuation and not complete), `empty-response` (declared complete with no component after an earlier usable one), `no-progress` (a round in which no requested file could be added: denied, duplicate, unreadable, or malformed requests), or a budget or provider reason. A usable earlier proposal is kept and never replaced by an empty reply. A reply that is not a JSON object gets the one repair attempt, then ends discovery as `provider-error` |
| Malformed response | One repair request within the request budget, then the model output is discarded and counted |
| Citation to an unknown excerpt, a path not in the snapshot, or an unrelated excerpt | The component or connection is rejected and counted by reason; the text is not stored |
| A component with no valid support | Rejected; a model cannot invent a component |
| Stored view whose IR hash, snapshots, or cited excerpt hashes no longer match | `stale`: not rendered, warning, deterministic output only |
| A view that fails revalidation | `invalid`: same handling, different message |
| Edited or unowned generated output | Existing ownership protection applies; nothing is overwritten or deleted |

Implemented in #50 (`repodocs/architecture/discovery.py`): the prompt, the response schema, the bounded loop (one repair attempt for a non-JSON reply, requests counted against `max_requests`, rounds against `max_rounds`), validation, persistence of the view with the context it was built from as one transition (the context is written to a new file named by its hash, the view is replaced atomically, and the previous context is removed only afterwards, so a failure leaves the previous pair whole), and revalidation on load (`current`, `stale` after a rescan, `invalid` when tampered or when it no longer reproduces). The model names components by name; IDs are computed by the engine. Rejection reasons recorded by count: `unknown-evidence`, `unrelated-evidence`, `invalid-path`, `no-valid-members`, `unsupported-component`, `unsupported-connection`, `unknown-component`, `ambiguous-component`, `unknown-repository`, `invalid-field`, `duplicate-*`, `self-connection`, `unconfirmed-candidate`, `unbound-candidate`, `malformed-*`, and the size caps (40 components, 80 connections, 20 alternatives).

Implemented in #51: `repodocs/architecture/render.py` projects a stored view into a typed diagram model (a single diagram up to 12 components, otherwise a grouped overview plus one detail diagram per group of two or more components) and writes `flowchart LR` Mermaid with entities for markup, visible `(proposed)`, `(low confidence)` and `(unconfirmed)` marks, dashed proposed edges, a legend and banner, and evidence IDs in comments. `validate` accepts only that grammar. Output files are optional artifacts named `architecture-view*`, labeled `repodocs-architecture`, and covered by the existing ownership, retirement, and no-op rules. The model has no deployment information, so deployment is not drawn. `repodocs/architecture/render_check.py` renders with the pinned renderer (`tools/mermaid`, `@mermaid-js/mermaid-cli` 11.17.0, `securityLevel: strict`, no HTML labels), screens the SVG for scripts, event handlers, embedded content, and external links, and reports `not-available` when the optional tooling or a browser is missing.

Implemented in #52 (`architecture/navigation.py`, `export.py`, `viewer_script.py`):
- `architecture-view-evidence.json` maps every component and connection to its cited locations (repository, path, line span, content hash, state) and member files, with a link only for a committed snapshot of a `github.com` repository, pinned to the exact 40-character SHA, with an encoded path (and the registered subdirectory prefix) and a `#L` anchor. The `origin` remote is read from configuration only, matched against github.com forms, and never copied into any output, so credentials in a CI checkout URL cannot leak. A working-tree snapshot has no link, is marked `working-tree`, and shows its fingerprint. The map holds no source text.
- `architecture-view.html` is optional (`architecture.html_export`), one self-contained file: the pinned Mermaid bundle (`mermaid.min.js` from `tools/mermaid`, embedded byte for byte) draws the diagram in the browser at `securityLevel: strict`; the page offers a diagram choice, zoom, pan, fit, clickable components and connections, and an evidence panel. A Content-Security-Policy allows only the two inline scripts by hash and no connection, image, frame, or form target. Text reaches the page through `textContent`, the data block escapes `<`, `>`, `&` and line separators, and links must match the allowlist. A project that asks for the page and lacks the bundle gets an error, never a different page, because Mermaid cannot draw without it and a differing page would churn. It was checked in Chrome: it draws, zooms, shows evidence on click, and records no CSP violation, request, dialog, or console error.
- Alternatives are shown, as ADR-026 requires: `architecture-view-alternatives.md` (written only when the view has alternatives, with each statement as plain escaped text, the components it concerns, and its evidence locations), an `alternatives` list in the evidence map (same order as the view, with located evidence), and an Alternatives section of the page, built with `textContent`. They stay out of the diagram so it does not crowd.
- The page, map and alternatives document are `architecture-view*` optional artifacts, so ownership, edit protection, retirement, and no-op detection apply.

### 7. Rendering, navigation, and publication (details settled in #51 to #53)

- The Mermaid source is produced deterministically from the stored view; the model supplies no markup. Proposed and low-confidence items are drawn differently (style and a legend) and cross-repository unconfirmed links are drawn dashed and labeled.
- A real renderer check uses `@mermaid-js/mermaid-cli`, pinned to an exact version in an optional tooling directory. It is required in CI and optional for users. A missing renderer is reported as "not checked", never as success.
- Source links are generated only for `github.com` repositories (the host comes from the repository's configured remote), pinned to the exact commit SHA, with encoded paths and line anchors. A snapshot whose `revision_kind` is `content` never gets a commit link and is labeled with its fingerprint.
- The static HTML export contains the diagram, an evidence map, zoom, pan, and fit-to-view. It has no backend, no remote assets, and no editor. All text is escaped, and URLs are validated against the allowlist.
- All new files are tracked in the artifact manifest with the label `repodocs-architecture`, so ownership, retirement, no-op detection, and publication (ADR-023) apply. Publication with no stored view keeps earlier architecture files as unverified instead of deleting them (section 5). The manifest's top-level `source_ir_hash` is the current one and each kept file keeps its own recorded per-artifact value, which is how mixed provenance is represented. Nothing in the manifest or the pull request describes a kept file as current.

Evaluation (#54): see `docs/evaluation/architecture-discovery/REPORT.md`. It found and fixed three defects (an inventory crowded out by data files, first-round slots spent on container files, and an unreadable dark-mode page), measured deterministic mode at zero application components on both subjects, and records two earlier live smoke tests with a 1.5B model (partial, different engine revisions, quality not accepted). The owner decision is pending.

### 8. Deterministic mode and its actual depth

Deterministic mode needs no key, network, or extra dependency and is unchanged. Its depth is: framework-aware facts for the supported stacks (endpoints, routes, requests, entities, tables, OpenAPI), and manifests, languages, and dependencies for anything else. For a stack without an adapter it produces a repository and language inventory and no component or connection model. It does not understand application responsibilities. Documentation says so, and no document implies otherwise.

### 9. Threat model

| Threat | Mitigation | Test |
|---|---|---|
| Instructions in a README or source steer the model | Text is data; schema-constrained output; validation never trusts the model's labels; no tools | Hostile-instruction fixtures (#49, #50) |
| The model requests secrets or files outside the snapshot | Resolver: deny list, snapshot membership, hash check, no links, budgets | Traversal, junction, symlink, stale, oversize (#49) |
| Secrets in excerpts reach a provider | Redaction before storage and send; separate consent; local-only routing; review step | Secret canaries (#49) |
| Consent widens silently | Separate setting bound to a policy version | Denied-consent and IR-only-consent tests (#49) |
| Runaway cost or loops | Round, request, file, byte, and response budgets; one repair retry | Budget exhaustion (#49, #50) |
| Fabricated or unrelated citations promote a claim | Citations resolve to excerpts of the same repository; classification computed, not trusted | Fabricated and unrelated citations (#50) |
| A model invents a cross-repository link | Cross-repository connections stay proposed unless a confirmed candidate is bound to that connection's actual components, direction, and evidence | Authority tests (#50) |
| Mermaid directives, click handlers, labels | Deterministic generation, entities for markup, validator rejects directives, real renderer check | Hostile labels in rendering (#51) |
| Script or unsafe URL in HTML or SVG | Escaping, URL allowlist, no remote assets, sanitized SVG, security level kept strict | Hostile HTML, javascript and data URLs (#52) |
| Misleading source links for dirty trees | Commit links only for `commit` snapshots | Dirty-tree tests (#52) |
| Stale results presented as current | Revalidation on load | Stale tests (#50, #53) |
| Overwriting user or edited files | Manifest ownership and hash preflight | Ownership tests (#51 to #53) |

### 10. Migration

Schemas are `1.0` while unreleased (ADR-014). A project without the new settings behaves exactly as before: no source consent, no view, no new files. Existing `ai-bundle`, `ai-interpret`, `architecture-ai.md`, claims, confirmations, and the manifest format are unchanged. New manifest entries use a new generator label, which older RepoDocs versions treat as an unknown optional artifact; a project is not expected to mix versions.

## Amendments

- **ADR-020 / D35.** "No source text" now applies to the evidence bundle and claims. Architecture discovery uses a separate context bundle that may contain bounded, redacted excerpts, under the consent and budgets above. IR-only consent never covers it.
- **ADR-021 / D36.** "AI cannot change diagrams" now applies to the deterministic documents and the IR. A separate, validated, inferred architecture view may drive separate diagram files. It still cannot add to or alter any observed fact, candidate, or confirmation.
- **ADR-023 / D38.** Publication from a clone with no stored architecture view keeps existing, owned, unedited `architecture-view*` files and says they are unverified, instead of removing them. A known-stale view and `repodocs generate --retire-architecture` still remove them. The owner authorized this policy for the hardening pass on 2026-11-17; the ADR text itself has not been reviewed yet.
- **ADR-004 / D19.** Unchanged: explicit consent, redaction, and local-only mode. This ADR extends consent to a second, named scope.

## Consequences

- Architecture understanding for unfamiliar stacks becomes possible with a local model, and with a cloud model after explicit consent. Its quality is unproven: the two saved live observations in #54 were not accepted. Nothing here claims a mock or replay proves live model quality.
- The cost is more state to keep honest: a second consent, a second persisted model, and a second set of artifacts.
- Redaction cannot be proven complete. Excerpts raise the stakes of that gap, which is why consent is separate, the bundle is reviewable first, and local models are the default recommendation.

### Awaiting owner

- Confirmation of the default budgets above.
- Whether a human review step (beyond reviewing the generated pull request) is required before a view is rendered. This ADR assumes the pull request is the review.
- A university or other OpenAI-compatible provider is out of scope. Its integration needs: a configurable base URL and model name; a credential read from the environment only; classification as external unless the host is loopback; no redirects; TLS verification on; a schema-constrained or JSON-mode response with the same validation; the same request, response, and timeout bounds; usage reporting if available; tests against a local server; and a decision on what that provider's retention terms permit. This is recorded for a later scope change, not implemented.
- Whether to reuse the project's Anthropic default model for discovery or recommend a smaller model. Neither is a dependency; the model is configuration.
