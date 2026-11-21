# ADR-034: Safe local and public GitHub web workspaces

- Date: 2026-11-20
- Status: Implemented for #70 under epic #63 (`src/repodocs/web/workspaces.py`); submitted for review.
- Issue: #70; depends on ADR-027 (#64) and the application seam (#69).
- Preserves: ADR-014 snapshot provenance, ADR-025 untrusted-repository hardening, D43.

## Context

The browser workflow must analyze approved local checkouts and public GitHub repositories without running their code, choosing server paths, or leaving unbounded data behind. Existing scanning already records commit or content-fingerprint provenance and inspects Git state with fsmonitor, hooks and filters disabled.

## Decision

- Add a backend-only workspace core. `WorkspaceManager.acquire(name, sources)` creates `<store>/ws-<random>/` holding `repodocs.json`, a backend-owned `destination/` (role `destination`, output under `docs/generated`), and any fetched `sources/<id>/` checkouts. It returns the ordinary `ProjectConfig` that the shared engine (#69) scans. It adds no analysis, HTTP routes, or UI.
- A request names a local source only by an operator-configured opaque name (`WorkspaceSettings.local_roots`); roots may not contain or sit inside the store. Local trees are read in place, never copied, written, or deleted, so uncommitted changes appear through the existing local snapshot fingerprint. Recorded state omits the local path. A non-Git tree never receives a commit SHA.
- A GitHub source is `owner`, `repository` and an exact lowercase 40-character commit SHA. Names must be canonical account/repository names, not URLs or paths. The only URLs are fixed: `https://api.github.com/repos/<owner>/<repo>/git/trees/<sha>?recursive=1` and `https://github.com/<owner>/<repo>.git`.
- Preflight before Git downloads anything: one unauthenticated HTTPS request with no proxy, credentials or redirects, a deadline and a response byte cap. The recursive tree must not be truncated and must satisfy limits for entries, files, per-blob size, aggregate bytes and path safety (no traversal, absolute, backslash, control, `.git`, drive/stream, trailing-dot or case-colliding paths). Symlinks, submodules and unknown modes are refused, not initialized. Blob sizes in an immutable commit tree bound source bytes up front; the disk monitor below is a second line, not the only one.
- Git runs with no shell and a scrubbed environment (no inherited Git variables, tokens or proxies; no global/system config; no prompt or askpass), `core.fsmonitor`, hooks path, attributes file and credential helper disabled, only HTTPS allowed, redirects off, `fsckObjects` on, no submodule recursion, an empty template, `fetch --depth=1` of the exact SHA and a detached checkout. A monitor polls the deadline and the checkout directory size and kills the process tree at either bound. After fetching, the commit's tree ID must equal the one GitHub listed. Provenance is then verified through `read_git_state` and `resolve_snapshot` in pinned mode: clean tree, HEAD equals the pin.
- Limits are explicit (`SourceLimits`, positive integers): network and Git time, response bytes, entries, files, file bytes, aggregate bytes, checkout disk, sources per workspace, workspace count and retention age.
- Each workspace has a marker file written first. Cleanup and retention delete a directory only when its ID matches `ws-<32 hex>`, it is a non-link directory directly under the store, and its marker has our kind and the same ID. Failed or interrupted acquisition removes its own workspace. Source failures are returned in `WorkspaceError.failures` (source label and message); a failed multi-source request creates no workspace.
- Acquisition success is recorded in the marker (source ID, kind, label, requested revision, Git state) so every participating revision is inspectable.

## Alternatives

- Cloning with a checked URL and measuring disk afterward lets a large repository download first; the tree preflight bounds it beforehand.
- Downloading a GitHub archive has no Git pin or verification and would invent provenance; it is not used.
- Implementing Git or an HTTP client was rejected: standard library `urllib` and the installed `git` are enough.
- Cloning into a local-source path or copying local trees would risk modifying human work or doubling data.

## Consequences and limitations

- Public GitHub only; no tokens, private repositories, other hosts, or archives. Unauthenticated API rate limits can refuse a request, and the message says so.
- Symlinks and submodules are refused rather than supported, so a GitHub repository containing either cannot be analyzed in this release. Large repositories above the defaults are refused, not sampled.
- The tree API reports blob sizes, so unusual packing is bounded by the disk cap and timeout rather than predicted. The monitor polls (about four times a second), so a fast writer can pass the cap briefly before it is killed. The checkout is not executed, but local roots are trusted only as far as the existing scanner hardening.
- Environment scrubbing drops proxy variables, so GitHub access behind a mandatory proxy is unsupported.
- Concurrent acquisitions are not serialized; the workspace-count limit is checked at creation and may be briefly exceeded under races. #72 runs one active job.
- Symlink-refusal tests for cleanup are skipped where the operating system does not allow creating links.
- Operator confirmation of the exposure policy and authentication remains #71; this ADR provides no access control.
