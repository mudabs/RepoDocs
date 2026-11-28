# ADR-029: Source context selection and a compact reference index

- Date: 2026-11-20; review update: 2026-11-21
- Status: Implemented in #66 (epic #63). Decision D45.
- Amends: ADR-026 section 3 (selection, inventory and budget accounting) and the discovery request it sends (reference index, coverage and a final request-size check). The consent, disclosure ceiling, deny list, redaction and actual-read bounds are unchanged.

## Context

The first-round context chose a README, every manifest and every file with an entry-point name, in repository order. On an application like DigitalBoneBox it spent the budget on a directory listing, data files, one-line re-exports and the first 200 lines of a long server file, and one repository could use the whole round before another was read. The inventory, the instructions and the metadata were not counted against the total budget, so the context could exceed the documented 200 KiB.

## Decision

### Selection

- Candidates are substantive text source and config files by name and location only. Assets, vendored directories, denied names, READMEs and manifests are not candidates. Test and example paths are demoted, role names (routes, controllers, services, models, components) are promoted, and size up to 6 KiB counts. A file under 350 bytes is dropped unless its name is an entry point, and a tiny entry point ranks after every substantive file, so a one-line re-export is kept only when a small repository has room.
- Each candidate has a layer from its name and location (`backend`, `frontend`, `storage`, `other`). The layer is a heuristic for balance and reporting. It is never a claim about what the file does.
- The first round takes, per repository: one language manifest, the best-ranked file, a short head of the README (40 lines), then the layers in turn, then remaining manifests (container files last). Repositories are served one file at a time in turn, so no repository can take the round. The round and total budgets still apply.
- A long source file contributes one block-aligned window (at most 200 lines and `max_excerpt_bytes`) holding the most definitions, route registrations, storage and request calls, instead of its first lines. Ties go to the earlier window. README and manifest excerpts are short heads. A model's explicit line request is unchanged.

### Budget

- `max_context_bytes` now bounds the excerpts, the inventory, the reference index, the diagnostics and a reserve for the instructions. `usage` reports `context_bytes` (excerpts), `metadata_bytes` and `reserved_bytes`, and an excerpt that would push the sum over the limit is refused as `budget-exhausted`.
- Excerpts and metadata are measured as they travel: JSON inside the user message, which the request body encodes again as a string, so quotes, backslashes and line breaks count twice. The figures stay estimates (indentation inside the message differs slightly, and the document fields the message leaves out are still counted); the final request check below is the bound.
- The omission list has the lowest priority. Refusals keep being recorded after an excerpt is admitted, so the list is cut, with `omitted_truncated`, whenever it would push the total over the limit, and `omitted_counts` keeps every reason's full count. An excerpt can therefore displace omission entries but never the inventory, index or coverage.
- The reserve is `min(6 KiB, max_context_bytes / 4)`, a constant sized for the discovery prompt, response schema and message envelope. A test fails if discovery's prompt and schema outgrow it. A budget smaller than the fixed overhead, about 3 KiB with the inventory on a small repository, yields no excerpts and says so.
- The inventory takes at most a fifth of the budget and `max_inventory_paths`. Assets (images, fonts, media, archives) and files beyond 12 in a directory are counted per directory (`assets`, `more_files`), never listed. Directories and files are listed together by depth, then name, so a root file precedes any nested directory. When the inventory exceeds its share, its longest list (paths, `more_files` or `assets`) is cut by a quarter at a time, shallowest entries kept, until it fits.

### Reference index

- Opened files of JavaScript, TypeScript, Python and Java contribute their literal static imports: `import ... from`, bare `import`, re-exports, `require` and `import()` whose whole argument is one string literal, Python `import` and `from ... import`, and Java `import`. Comments, string contents, template or computed paths (`require('./plugins/' + name)`), aliases and other languages contribute nothing.
- The parsers are the existing ones, never a new line pattern: JavaScript and TypeScript reuse the analyzers' comment- and string-masked import scanner (ADR-016), Python uses the standard-library `ast` module, which parses without executing, and Java masks comments, strings and text blocks before reading `import` declarations. Import-looking comments and examples are therefore not references. Unsupported or malformed syntax can leave references unreported, and the JavaScript scanner's regex-literal limits still apply; the index is a bounded structural listing, not a complete dependency graph.
- The whole file already read within `max_file_bytes` and the hash check is parsed, so a declaration that spans an excerpt boundary is read correctly, but only references on excerpted lines are listed: the index discloses nothing about lines the context does not show (ADR-026), and no unopened file contributes. A later excerpt of the same file adds the references on its lines. Each file entry carries its repository, path and content hash. A reference carries line, specifier, and a target that is a snapshot file or null.
- References are resolved once, when the file is read, and a Java class resolves through a per-repository table of path suffixes, so a large repository is not scanned per reference and rebuilding the context resolves nothing again.
- A target is `read` (the whole file is in the context), `excerpted` (a span), `unopened` (in the snapshot, not in the context) or `unresolved` (a package, alias, missing or outside path; a denied file is also `unresolved` and never named). The index is a list of what files say, sorted by line. It records no order, call, or data flow and says so in its note.
- At most 30 references per file and 120 characters per specifier. Specifiers are redacted. The index share of the budget is an eighth.

### What discovery sends

- Each discovery request carries the redacted reference index and the coverage diagnostics with the excerpts, so the model sees what was opened and what was not, instead of the preview alone holding them.
- Before every request a final local check measures the complete request: the provider's own `request_body` when it offers one (preserved through the service's cancellation wrapper and the usage ledger, ADR-030 and ADR-031), otherwise the portable system, user message, schema and model envelope. A request larger than `max_context_bytes` is not sent and not counted; discovery ends `partial` with `context-budget`, or fails when nothing usable exists. The reserve and metadata figures in the preview are estimates and do not replace this check. No new disclosure ceiling or consent scope is introduced.

### Diagnostics

- `coverage` per repository: candidate and opened file counts, per-layer counts, `missing_layers` (a layer that has candidate files by name and none opened) and the best five unopened candidates. A layer with no files is not reported, so no component is implied.
- `omitted` lists material reasons first and at most 100 entries, with exact `omitted_counts` per reason and `omitted_truncated`. Large entry points and role files over the read bound are reported as `too-large`, and so is a requested span whose first line alone exceeds `max_excerpt_bytes`: nothing would be shown, so nothing is opened or counted.

### The saved context

- A stored view is saved with the context of the request whose reply produced its proposal, exactly as sent. Files fetched after that request, which the model never saw, are not in it, and the view's `context_hash` names it. Refusals are recorded in the context of the next request, which also tells the model what is not available. Refusals after the last request are not kept with the view.

### Unchanged

Consent (`ai.source_excerpts_consent`, `SOURCE_POLICY_VERSION`), disclosure ceilings, the deny list, redaction version, link and path safety, hash checks and the one-extra-byte read bound. `SELECTION_VERSION` is `1.1`, so a view stored from a `1.0` context is `invalid` on load and needs `architecture discover --refresh`. `context_version` stays `1.0` and `schemas/architecture-context-1.0.json` gains the fields above while unreleased (ADR-014).

## Alternatives

- **Read every file to build a complete import graph.** Rejected: it would read and disclose beyond the file and byte budgets.
- **Rank by import in-degree.** Rejected for the first round: it needs files to be read first. Later rounds can request unopened targets the index names.
- **Parse with a language server or tree-sitter.** Rejected: a new dependency and a larger attack surface for literal imports. The existing masked JavaScript scanner, Python's `ast` and the masked Java reader are bounded, already reviewed and never execute code.
- **Literal line patterns for imports.** Rejected after review: they read commented-out or quoted imports as references.
- **Sample data files for the storage layer.** Rejected: sampled rows disclose data, not architecture, and pattern redaction is not complete.
- **Count only excerpts against the budget.** Rejected: it let the real total exceed the documented limit.

## Consequences

- Behavior, not manifests or data, fills the first round, and a repository outside the first in order is read.
- Layers and rankings are heuristics from names; a repository with unusual layout may rank poorly. `coverage` shows what was and was not opened instead of hiding it.
- Real usefulness against the pinned DigitalBoneBox and Flaskr sources is not claimed here. #54 and #77 remain the gate, and fixtures only show selection shape.
- Out of scope: provider, configuration, CLI and service changes, and token-based budgets (#67). The discovery loop changes only in what it sends and in the final size check above.

## Amendment (#77 verification): context fitted to the model's input allowance

- Found during live #77 verification: EventShare at `93970207` produced 8 excerpts (43,773 context bytes) whose first
  request measured 92,643 byte-bound tokens, against a 32,768-token window with 16,000 tokens reserved for output.
  Preflight refused correctly, but the context budget (`max_context_bytes`, 200 KiB) did not depend on the model.
- When a provider is known, `service.fitted_context_bytes` measures the first request with the same byte bound as
  preflight, on the portable request envelope. If it does not fit the allowance (window minus reserved output minus
  a 512-token margin for provider-specific fields), the total is stepped down and measured again. A context that
  already fits gets no override, so its hash, consent and stored views are unchanged.
- A fitted total shares excerpts across at least 14 files (each at least 768 bytes) and gives the path inventory a
  tenth of the budget instead of a fifth, so the first round still spans repositories and layers. It bounds the
  source context only; the whole request keeps the configured cap, and the run ledger still admits every request
  against the real window.
- The total is never fitted below 12 KiB (`MIN_USEFUL_CONTEXT_BYTES`). If that still does not fit, preflight refuses
  as before and says the context was already at its smallest useful size; nothing is sent.
- The budget depends only on the project's resolved `ai.limits` for the provider name, so CLI and web previews, job
  admission, the job's own re-preview and discovery compute the same context and hash.
- The preview reports `context_fit` before consent: whether the context was fitted, the fitted total, opened versus
  candidate files, and layers with candidates but no excerpt. The home page shows it in the disclosure review and,
  for a local model without a review step, beside progress and the result.
- EventShare with these limits: 15,371 byte-bound input tokens (15,479 with the Ollama request body) plus 16,000
  reserved output fits 32,768. 4 of 230 candidate files are included (README, `pom.xml`, one backend service, one
  frontend component); the storage layer is not covered. A smaller output reserve (`--request-output-tokens`) leaves
  more room for source; the default reserve is unchanged here.
