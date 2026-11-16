# ADR-025: Untrusted Input and Data Boundary Hardening

- Status: Accepted, except the items under "Awaiting owner approval"
- Date: 2026-11-16

## Context

AGENTS.md says repositories are untrusted: their code is not executed and their secrets are not leaked to model providers. ADR-004 and ADR-020 settle consent and redaction for external AI, ADR-005 settles that analyzers do not run repository code, and ADR-006 and ADR-022 settle generated output. Issue #27 asks that these boundaries be tested against hostile inputs and fail closed.

Probing the implementation with hostile fixtures found real gaps, not only missing tests:

- `git status`, which every scan runs, executes programs named in the scanned repository's own `.git/config`: `core.fsmonitor` and any `filter.<name>.clean`, `smudge`, or `process`. A repository could run code on the machine that scanned it.
- A failing `git status` was read as a clean tree, so evidence could be tied to a commit that the files did not match.
- One deeply nested `package.json` or `pyproject.toml` aborted the whole scan with "Unexpected error".
- Text taken from analyzed source was written into Markdown tables and code spans and into Mermaid labels without enough escaping. A backtick ended a code span, `<img>` and `[x](y)` were emitted raw, and a Mermaid label could close its own quote.
- A request to a loopback Ollama host followed `HTTP_PROXY` and so reached another host without the consent that sending it there needs, and a redirect was followed.
- Redaction missed common token shapes and keys with a prefix such as `client_secret` or `DB_PASSWORD`.

## Decision

### Scanning never runs repository code
- Git is run with `--no-optional-locks`, `core.fsmonitor=false`, `core.hooksPath` pointing at nothing, and every `filter.<name>.clean`, `smudge`, and `process` command that the configuration defines set to empty and `filter.<name>.required` set to false. Reading the configuration is safe. Without these, `git status` runs `core.fsmonitor`, filter commands, and the `post-index-change` hook when it refreshes the index. Submodule status is checked with `--ignore-submodules=dirty`: a submodule at another commit still makes the snapshot dirty, but git does not start a process inside the submodule, whose own configuration would otherwise be trusted (verified with a filter configured in a submodule).
- If `git status` fails, the scan stops with a `SnapshotError` that names the cause. It never reports a clean tree it could not read.
- A directory that resolves outside the repository, such as a Windows junction, is skipped with a warning. Symlinks, special files, binary files, and files over the size limits are handled as before. Evidence paths are checked against the repository root by the golden harness (ADR-024).
- The destination repository may be a scanned one. Generated artifacts are written, read, and removed only through `owned_path`, which refuses a path that leaves the output directory (absolute, `..`, or through a link) and a link at an artifact name. `validate` applies the same rule to the paths in the artifact manifest.

### Hostile contents degrade one file, not the scan
A manifest or analyzer input that cannot be parsed, including input nested deeper than the interpreter allows, is skipped. The rest of the repository is still analyzed. The existing fixture and file-count limits still fail the scan with a message that names the limit.

### Generated text is inert
- Text from analyzed source is written through two helpers. `markdown_code` produces a code span whose fence is longer than any backtick run inside it. `markdown_text` backslash-escapes characters that can start a link, image, tag, code span, or table cell. Both turn line breaks and control characters into spaces.
- Mermaid labels use Mermaid's own entities for `"`, `<`, and `>` (`#quot;`, `#lt;`, `#gt;`) in place of backslash escapes that Mermaid does not understand. Every `%%` comment line is scrubbed of line breaks so a name cannot start a new statement.
- `repodocs validate` rejects, outside code spans, raw HTML, link and image syntax, and reference links in Markdown, and rejects Mermaid directives (`%%{`) and markup in labels and comments of attributes and relationships. A hand edit that adds a link to a generated document fails validation.
- Markdown is checked the way a reader sees it, in one left-to-right pass, so an escaped backtick does not open a code span. The AI document follows the same rule.
- Plain text such as `javascript:alert(1)` is not rejected. It is not active without link syntax, and GitHub does not autolink that scheme.

### External AI boundary
- Redaction rules are version 1.1. They add GitHub fine-grained, GitLab, npm, Slack, Google API, and Stripe keys, and they recognize secret assignments whose key has a prefix (`client_secret`, `DB_PASSWORD`, `x-api-key`). The prefix is bounded so a long unbroken word cannot make matching quadratic. Redaction is still pattern-based and not a completeness guarantee.
- A request to a loopback host never goes through a proxy configured in the environment. No provider request follows a redirect.
- Loopback detection is unchanged and is tested against hostnames that merely start with or contain `127.0.0.1` or `localhost`, credentials in the URL, decimal addresses, and a trailing dot. IPv4-mapped IPv6 addresses are not tested because Python versions disagree about them; older versions treat them as external, which fails closed. Anything not an exact loopback host is external and needs recorded consent.

## Consequences

- Generated documents show analyzed names in code spans or escaped text. A name containing backticks or markup is visible as text and is not active.
- Fixtures cover symlinks and junctions, file and size limits, hostile configuration, hostile contents, hostile names in generated output, secrets in the bundle, and consent. They run on Linux and Windows.

### Awaiting owner approval
- **Uncommitted edits inside a submodule are not noticed.** Submodule files are scanned, but the snapshot is not marked dirty for an uncommitted edit inside one, so a local snapshot can name the superproject commit while the scanned submodule files differ from it. Noticing them needs a git process inside the submodule, which would run that submodule's configured programs. A pinned scan of a checkout whose only change is such an edit is accepted. Alternatives are refusing repositories that have submodules in pinned mode, or hashing submodule files into the fingerprint. A test pins the current behavior.
- **The Mermaid entities** `#quot;`, `#lt;`, and `#gt;` follow Mermaid's documented syntax but have not been checked with a real Mermaid renderer, which is not available (ADR-022).
- **Redaction of the deterministic documents.** ADR-004 requires redaction for external AI only. A URL literal in analyzed source that contains a secret is written to the generated documentation and so to the documentation pull request. Applying `redact_text` to generated documents would change deterministic output and is a policy decision.
- **Git run by `publish`.** Publication runs git in the user's destination repository, which is trusted by design (ADR-023). If a destination can be untrusted, the same inert options should apply there.
- **A sandbox for external analyzers** is still future work (ADR-005); no external analyzers exist yet.
- **Resource limits beyond file size and count**, such as a per-file parse time limit, are not implemented. The probes did not find an input that took more than a second, so none was added.
