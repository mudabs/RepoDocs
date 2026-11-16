# ADR-019: Candidate Confirmation, Rejection, and Stale Decisions

- Status: Accepted
- Date: 2026-11-15

## Context

ADR-018 stores every cross-repository match as an inferred candidate. ADR-003 and D17 require explicit confirmation before a match is authoritative, and ADR-010 reserves exit code 5 for unresolved required confirmations. Issue #18 asks for noninteractive confirm and reject workflows, decisions tied to repository revisions, stale handling when sources change, and the CI failure.

## Alternatives

- Store decisions inside `.repodocs/ir.json`. Rejected: a rescan rebuilds the IR, and human input should be reviewable on its own.
- Stale a decision whenever a participating repository has a new revision. Rejected: every unrelated commit would force every link to be confirmed again.
- Stale a decision only when the evidence the candidate rests on changes, and record the revisions for audit.
- Make the confirmation failure opt-in for CI. Rejected: ADR-010 asks for safe defaults.

## Decision

- Decisions live in `repodocs.confirmations.json` next to `repodocs.json`, with schema `schemas/confirmations-1.0.json`. The file is meant to be committed and reviewed. Each record holds the candidate ID, `confirmed` or `rejected`, the source and target node IDs, the candidate's evidence IDs, both participating snapshot revisions at decision time, and an optional note. No timestamps are stored, so the file is deterministic; version control supplies history.
- `repodocs candidates [--status unresolved|candidate|confirmed|rejected|stale|all]` lists candidates from the last scan. The default lists unresolved ones: no decision, or a stale decision. It also reports decisions whose candidate no longer exists.
- `repodocs confirm <id>...` and `repodocs reject <id>...` record decisions noninteractively, with an optional `--note`. Unknown IDs fail with exit code 2 and nothing is written. A later decision for the same candidate replaces the earlier one; that is the override.
- Candidate `status` in the IR is `candidate`, `confirmed`, `rejected`, or `stale`. `scan` applies the recorded decisions, and `confirm` and `reject` update the persisted IR. A candidate with a decision carries a `confirmation` object with the decision, the revisions it was made against, and the note.
- A decision holds while it is bound to the candidate: the recorded source and target node IDs and participating repositories match, and the candidate's evidence IDs equal those recorded. A record with an unknown revision kind, an empty revision, or duplicate evidence IDs is rejected with exit code 2. Evidence IDs include the file content hash, so any edit to a participating source file makes the decision `stale`. A stale candidate keeps its old decision visible but counts as unresolved until decided again, which rebinds it to the current evidence and revisions.
- A confirmed candidate stays `inferred` in classification and stays in `candidates`. Confirmation records human authority; it does not claim the link was observed in source.
- Every candidate requires a decision. `repodocs validate` checks artifacts first, then exits with code 5 while any candidate is unresolved or stale. `--allow-unresolved` skips that check for local drafts. `validate`, `generate`, and `candidates` fail with code 3 when persisted candidate states differ from what the current decisions file produces, so an edited or deleted decision is never listed or rendered as still in force; `scan` reapplies the file. `confirm` and `reject` start from the current file.
- Generated Markdown shows each candidate's status. Because statuses are part of the IR, a decision changes the IR hash and documentation must be regenerated before it validates.

## Consequences

CI that runs `scan`, `generate`, and `validate` fails closed on new or changed cross-repository links until a person decides them. Low-confidence candidates must be rejected explicitly, which is deliberate for now; a confidence threshold or bulk decisions can be added later without changing the file format. Confirmed links are not yet drawn as Mermaid edges; that belongs with diagram work. The IR schema stays at 1.0 while unreleased (ADR-014).
