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
- The reserve is `min(6 KiB, max_context_bytes / 4)`, a constant sized for the discovery prompt, response schema and message envelope. A test fails if discovery's prompt and schema outgrow it. A budget smaller than the fixed overhead, about 2 KiB plus the inventory, yields no excerpts and says so.
- The inventory takes at most a fifth of the budget and `max_inventory_paths`. Assets (images, fonts, media, archives) and files beyond 12 in a directory are counted per directory (`assets`, `more_files`), never listed, and shallow paths survive truncation.

### Reference index

- Opened files of JavaScript, TypeScript, Python and Java contribute their literal static imports: `import ... from`, bare `import`, re-exports, `require` and `import()` with a string literal, Python `import` and `from ... import`, and Java `import`. Comments, string contents, template or computed paths, aliases and other languages contribute nothing.
- The parsers are the existing ones, never a new line pattern: JavaScript and TypeScript reuse the analyzers' comment- and string-masked import scanner (ADR-016), Python uses the standard-library `ast` module, which parses without executing, and Java masks comments, strings and text blocks before reading `import` declarations. Import-looking comments and examples are therefore not references. Unsupported or malformed syntax can leave references unreported, and the JavaScript scanner's regex-literal limits still apply; the index is a bounded structural listing, not a complete dependency graph.
- References come from the whole file already read within `max_file_bytes` and the hash check, not only from the excerpt, and from no unopened file. Each file entry carries its repository, path and content hash. A reference carries line, specifier, and a target that is a snapshot file or null.
- A target is `read` (the whole file is in the context), `excerpted` (a span), `unopened` (in the snapshot, not in the context) or `unresolved` (a package, alias, missing or outside path; a denied file is also `unresolved` and never named). The index is a list of what files say, sorted by line. It records no order, call, or data flow and says so in its note.
- At most 30 references per file and 120 characters per specifier. Specifiers are redacted. The index share of the budget is an eighth.

### What discovery sends

- Each discovery request carries the redacted reference index and the coverage diagnostics with the excerpts, so the model sees what was opened and what was not, instead of the preview alone holding them.
- Before every request a final local check measures the complete request: the provider's own `request_body` when it offers one (preserved through the service's cancellation wrapper and the usage ledger, ADR-030 and ADR-031), otherwise the portable system, user message, schema and model envelope. A request larger than `max_context_bytes` is not sent and not counted; discovery ends `partial` with `context-budget`, or fails when nothing usable exists. The reserve and metadata figures in the preview are estimates and do not replace this check. No new disclosure ceiling or consent scope is introduced.

### Diagnostics

- `coverage` per repository: candidate and opened file counts, per-layer counts, `missing_layers` (a layer that has candidate files by name and none opened) and the best five unopened candidates. A layer with no files is not reported, so no component is implied.
- `omitted` lists material reasons first and at most 100 entries, with exact `omitted_counts` per reason and `omitted_truncated`. Large entry points and role files over the read bound are reported as `too-large`.

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
