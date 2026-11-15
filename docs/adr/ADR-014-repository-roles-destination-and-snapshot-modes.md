# ADR-014: Repository Roles, One Documentation Destination, and Two Snapshot Modes

- Date: 2026-11-14
- Status: Accepted

## Context

ADR-009 settles that system documentation is published through one coordination repository, and ADR-010 settles versioned JSON output with explicit exit codes. The Milestone 1 configuration still wrote generated output relative to the project root and stored a free-form repository `role`, and scans recorded Git state without stating what evidence resolved to. Issues #12 and #13 ask for an explicit destination contract and for separate local and pinned snapshot behavior before framework adapters build on the IR.

## Alternatives

- Keep writing output under the project root and treat the root as the implicit destination.
- Allow several destinations, one per source repository, with per-repository output directories.
- Model the destination as one explicit repository that is either a source repository or a separate destination-only repository.
- Record Git state only, and let consumers decide whether a dirty tree is acceptable.
- Make snapshot mode an explicit scan input with mode-specific acceptance rules.

## Decision

### Repository roles and destination
- `repositories[].role` is `source` or `destination`. Source repositories are scanned into the System IR. A destination-only repository is never scanned.
- `documentation.destination.repository_id` names exactly one registered repository that receives generated system documentation. It may be a source repository or the single destination-only repository. `repodocs set-destination <id>` promotes an already registered repository; `--replace` switches away from a previous destination, and a registered destination-only repository cannot be bypassed.
- `documentation.output_directory` is relative to the destination repository root and must resolve to a dedicated subdirectory inside it. When the destination is also a source repository, that subdirectory is excluded from its own scan.
- `repositories[].pinned_revision` is an optional full 40-character commit SHA. Generation fails when a pinned destination checkout does not match.
- The artifact manifest records the destination (repository ID, role, configured path, output directory, Git revision, working-tree state, pinned revision). The destination identity is the repository ID, role, configured path, and output directory together. Validation fails when the configured identity differs from the manifest in any of those fields, and generation refuses to write anywhere other than where the manifest records; revision and working-tree state may change between runs.
- `generate` and `validate` fail with a versioned JSON error and exit code 2 when the destination is missing, unknown, not a directory, or when the output directory escapes it.

### Snapshot modes
- `scan --snapshot-mode local` (default) scans the working tree including uncommitted and untracked files, and records `git_revision`, `git_branch`, `head_state`, `working_tree_state`, `git_status_fingerprint`, and a `content_fingerprint`.
- `scan --snapshot-mode pinned` accepts only a Git repository with at least one commit, a clean working tree, and an explicit pin for every source repository. Detached checkouts are accepted. Non-Git trees, dirty trees, and unpinned repositories are rejected.
- A pin (`pinned_revision` in configuration, or `--pin ID=SHA` for one run) must be a full lowercase 40-character SHA; abbreviated values are rejected as ambiguous. A HEAD that differs from the pin is rejected as stale, in both modes.
- Every snapshot states `revision_kind` and `revision`: `commit` with the HEAD SHA when the scanned content is exactly that commit, otherwise `content` with the working-tree fingerprint. Every evidence record carries the same `revision` and `revision_kind`, so evidence never claims a commit it did not observe.
- Snapshot rejections exit with code 6, distinct from usage (2), validation (3), and ownership (4) failures.
- A registered path inside a larger Git repository is tracked by that repository's commit with a `git_path_prefix`; dirty-state detection is limited to the registered subtree.

## Consequences

The configuration, IR, and manifest schemas gain required fields while remaining at version 1.0, because nothing has been released; from the first tagged release onward such changes require a version bump. GitHub Actions must run `scan --snapshot-mode pinned --pin <id>=<sha>` for every source repository and fail closed on wrong, dirty, or unpinned checkouts. Snapshot headers include branch and dirty-state metadata, so the IR hash, and therefore the manifest, changes between a dirty local scan and a clean pinned scan of identical content; that is intended, since the evidence revision differs. Branch names also enter the IR hash, which can mark a regeneration as changed without content changes; excluding volatile metadata from the manifest source hash is left to a later decision. Remote-only destinations configured by URL rather than a local path are not covered and belong with the GitHub automation phase.
