# Claude instructions for RepoDocs

## Before changing code

- Read `AGENTS.md`, `README.md`, and all relevant files under `docs/`.
- Read the GitHub issue completely, including its scope, acceptance criteria, dependencies, and exclusions.
- Treat the issue as the source of truth for the task. Do not expand scope without approval.
- Inspect the current repository state before editing.

## Implementation principles

- Keep code simple, clear, and sound.
- Prefer the smallest complete implementation that satisfies the issue.
- Avoid inflated code, speculative abstractions, unnecessary wrappers, and premature generalization.
- Prefer established, small dependencies over custom replacements.
- Preserve existing architecture and decisions unless the issue explicitly changes them.
- Do not invent unsupported behavior, relationships, coverage, or guarantees.
- Do not execute code from repositories being analyzed.

## Testing and documentation

- Add focused tests for new behavior, edge cases, failures, provenance, and safety boundaries.
- Run the relevant test suite before reporting completion.
- Update documentation and ADRs when an implementation changes an architectural contract.
- Report limitations and incomplete work explicitly.

## Git and pull requests

- Work on a feature branch and submit changes through a pull request.
- Keep commits focused and use clear commit messages.
- Never add `Co-authored-by: Claude`, Claude attribution, or any equivalent co-author trailer to commits, pull requests, or generated metadata.
- Do not modify unrelated files or silently overwrite human-authored content.
