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
- Git is run with `--no-optional-locks`, `core.fsmonitor=false`, `core.hooksPath` pointing at nothing, and every `filter.<name>.clean`, `smudge`, and `process` command that the configuration defines set to empty and `filter.<name>.required` set to false. Reading the configuration is safe. Without these, `git status` runs `core.fsmonitor`, filter commands, and the `post-index-change` hook when it refreshes the index. If the configuration cannot be listed, the scan stops, because the filters could not be switched off.
- **A snapshot is the exact commit only when every file the scan can read is the commit's content.** The superproject is checked with `git status --ignore-submodules=all`, so Git never starts a process inside a submodule. Each submodule is then inspected by its own call with the same hardening, recursively to a depth of 8. The snapshot is `dirty` (local scans get content-fingerprint provenance, pinned scans are refused with the reason) when a submodule is at a commit other than the one recorded, has modified, staged, or untracked files, or when a nested submodule does; when a submodule path is a link or a file, or holds files in a directory that was never initialized; or when a file that Git ignores is analyzed (ignored directories the scan skips by name do not count). A submodule that is not initialized contributes no files and is reported as a scan warning, not silently dropped. A nested depth beyond 8 makes the snapshot dirty.
- If any git command used for inspection fails, the scan stops with a `SnapshotError` that names the cause. It never reports a clean tree it could not read. A directory that is not a Git repository, or a machine without git, gives content provenance, and pinned mode refuses it.
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
- Neither provider follows redirects, and each has a test against a local server. Ollama uses an opener without a redirect handler. Anthropic uses the SDK's client built with `follow_redirects=False` and used as a context manager so the transport is closed. A 3xx response becomes a `ProviderError` (exit 7) that names the status and says it was not followed; it contains no credential, response body, address, or repository content, and nothing is sent to the redirect target.
- A request to a loopback host never goes through a proxy configured in the environment. An external provider still honors the environment's proxy settings, because that is how a corporate network reaches it, and the bundle is sent only after consent.
- Loopback detection is unchanged and is tested against hostnames that merely start with or contain `127.0.0.1` or `localhost`, credentials in the URL, decimal addresses, and a trailing dot. IPv4-mapped IPv6 addresses are not tested because Python versions disagree about them; older versions treat them as external, which fails closed. Anything not an exact loopback host is external and needs recorded consent.

## Consequences

- Generated documents show analyzed names in code spans or escaped text. A name containing backticks or markup is visible as text and is not active.
- A scan of a checkout with an ignored but analyzed file is no longer pinned-eligible, which can affect CI that leaves such files in the working tree. The refusal names the file.
- Fixtures cover symlinks and junctions, file and size limits, hostile configuration, hostile contents, hostile names in generated output, secrets in the bundle, and consent. They run on Linux and Windows.

### Awaiting owner approval
- **The Mermaid entities** `#quot;`, `#lt;`, and `#gt;` follow Mermaid's documented syntax but have not been checked with a real Mermaid renderer, which is not available (ADR-022).
- **Redaction of the deterministic documents.** ADR-004 requires redaction for external AI only. A URL literal in analyzed source that contains a secret is written to the generated documentation and so to the documentation pull request. Applying `redact_text` to generated documents would change deterministic output and is a policy decision.
- **Git run by `publish`.** Publication runs git in the user's destination repository, which is trusted by design (ADR-023). If a destination can be untrusted, the same inert options should apply there.
- **A sandbox for external analyzers** is still future work (ADR-005); no external analyzers exist yet.
- **Resource limits beyond file size and count**, such as a per-file parse time limit, are not implemented. The probes did not find an input that took more than a second, so none was added.
