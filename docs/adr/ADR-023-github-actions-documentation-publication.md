# ADR-023: Publish Documentation from GitHub Actions Through One Bot Branch

- Status: Accepted, except the items under "Awaiting owner approval"
- Date: 2026-11-16

## Context

ADR-009 settles one coordination repository for system documentation and documentation pull requests, ADR-008 settles full regeneration with no-op detection, ADR-010 settles that CI fails on unresolved confirmations, and ADR-014 settles pinned clean snapshots. Issue #25 asks for a GitHub Actions run that analyzes pinned revisions with explicit credentials, publishes without noise, updates an existing pull request idempotently, and cannot loop, race, or leak secrets to forks. Exit criteria for epic #9 add unresolved confirmations and concurrent runs.

An experiment with a coordination repository, two source repositories, and fresh clones found three things the design had to address:

- A clone that has the generated documentation but not `.repodocs/artifact-manifest.json` fails `generate` with exit code 4, so the manifest must be committed.
- The manifest recorded the destination's own Git revision and tree state. A documentation commit moves them, so every merged documentation pull request changed the manifest, which produced another pull request, indefinitely.
- Sources checked out on a branch instead of a detached SHA change the IR hash, because branch and head state are part of each snapshot.

## Alternatives

- Hide the self-trigger with `paths-ignore` alone. Rejected: it only suppresses the push trigger, not the schedule or dispatch triggers, and the manifest would still differ.
- Commit directly to the default branch. Rejected: ADR-009 and D09 require reviewable pull requests.
- One branch per run. Rejected: it creates a pull request per source change and cannot be updated idempotently.
- Implement publication in workflow YAML. Rejected: it could not be tested without GitHub, and YAML would hold the safety logic.
- A separate persistent service. Rejected by D04.

## Decision

### Manifest
- The artifact manifest records the destination's repository ID, role, configured path, output directory, and pinned revision. It no longer records the destination's Git revision or working-tree state. `generate` still reports those live facts in its response. This amends ADR-014.
- The manifest is committed with the documentation. The other files under `.repodocs/` are scratch state and are not.

### `repodocs publish`
- `publish` validates the output first, requires every cross-repository candidate to be resolved (exit code 5 otherwise), and then builds one commit on top of the checked-out base that contains only the output directory and the manifest. It uses a throwaway index, so the working tree, the checked-out branch, and the base are never changed.
- If that commit's tree equals the base, the run is a `no-op` and never contacts the remote or GitHub. This is the no-op rule of ADR-008.
- Otherwise it reads the bot branch (`repodocs/docs-update` by default), and if the branch already holds an identical tree it pushes nothing (`unchanged`). It pushes with `--force-with-lease` against the tip it read, so a branch moved by someone else in between is never overwritten.
- It finds an open pull request for the branch and base. It creates one if none exists and updates the existing one only when it pushed new content, so reruns are idempotent. A failure after the push is recovered by the next run, which sees the same tree and creates the missing pull request.
- The branch is bot-owned and one commit ahead of the base. A rejected pull request should be closed and its branch deleted, otherwise the next run opens it again.
- The documentation commit is built on the checkout, so publication requires the base branch to be checked out by name and refuses a detached HEAD or any other branch; otherwise another branch's commits would enter the pull request. The example passes the branch the run started on, which is the default branch for schedule, `repository_dispatch`, and pushes. Fetching the bot branch is depth-limited only when the clone is already shallow, so a complete clone is never made shallow.
- Publication refuses to run when the destination has changes outside the generated paths, and when the manifest is not inside the destination repository, so that documentation changes always target the configured destination.
- Exit code 8 reports a publication failure. `--dry-run` computes the change without contacting anything.
- The pull request description is built from the IR and the list of changed files and passed to `gh` by file. Nothing from source repositories reaches a command line or a workflow expression.

### Credentials
- The token reaches git through its environment configuration for https remotes only, and reaches `gh` through `GH_TOKEN`. It is never written to disk, to a remote URL, or to a process argument list, and every error message is redacted before it is shown.
- The workflow's own `GITHUB_TOKEN` stays read-only. A separate token with contents and pull request write access to the coordination repository, and contents read on sources, is supplied by the repository owner.

### Workflow
- `.github/actions/publish` is a composite action. It installs RepoDocs from its own revision, so engine and workflow cannot drift, then runs `scan --snapshot-mode pinned`, `generate`, `validate`, and `publish`. Steps use `shell: bash` so `pipefail` applies, and none pipes the CLI's output.
- Pins reach the action as a JSON map and never come from the checkouts. The example workflow resolves each branch to a SHA through the API once, then checks out that exact SHA, so sources are detached at the pinned commit and a push in between cannot change what is analyzed.
- The example caller (`docs/examples/repodocs-publish.yml`) is triggered by `workflow_dispatch`, `repository_dispatch`, a schedule, and pushes that touch anything but the generated paths. It never uses `pull_request` or `pull_request_target`, so fork code cannot reach the token. It serializes runs with a concurrency group that queues instead of cancelling.
- `publication-selftest` runs the action in dry-run mode on a pull request against a throwaway repository, with no secrets, and checks that a wrong pin fails the action.

### Loop prevention
Five independent layers: the manifest no longer changes when the destination moves; a merged documentation pull request touches only paths the push trigger ignores; the output of the next run is then a no-op; a fixed bot branch cannot multiply pull requests; and a concurrency group runs one publication at a time.

### Decisions awaiting owner approval (O05)
These are least-privilege defaults, not settled choices. O05 stays open until they are confirmed:
- Triggers: dispatch, schedule, push to the default branch, and `repository_dispatch` from sources, which needs a small workflow in each source repository.
- A single token secret for both reading sources and writing the coordination repository, rather than a GitHub App installation token minted per run.
- The name of the bot branch and the title of the pull request.
- Stale or absent AI claims delete `architecture-ai.md` from the pull request instead of being preserved.

## Consequences

Merging a documentation pull request does not cause another one, and a source change that does not alter the output creates nothing. A source change that adds a cross-repository candidate fails the run with exit code 5 until a person decides it in the coordination repository, which is the intended gate. Real pull request creation depends on the GitHub CLI and a real token, so it is covered by a stand-in in tests and is not exercised by the self-test. The IR and confirmations bind to content hashes, so decisions recorded on a checkout whose line endings were converted (for example Windows with `core.autocrlf`) go stale on a Linux runner and fail the gate; record decisions from a checkout with unconverted line endings. AI interpretation does not run in CI, since it needs consent and a key; documentation that depends on stored claims is removed when the claims no longer match, and the pull request says so. Only github.com-style https remotes receive the token, which also covers GitHub Enterprise Server hosts by name, but that is untested.
