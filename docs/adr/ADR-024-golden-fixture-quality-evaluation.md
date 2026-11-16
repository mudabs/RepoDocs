# ADR-024: Golden-Fixture Quality Evaluation

- Status: Accepted, except the items under "Awaiting owner approval"
- Date: 2026-11-16

## Context

ADR-011 settles that quality is measured with golden fixtures, fact-level precision and recall, provenance checks, stability checks, and deterministic-versus-AI comparisons. Issue #23 asks for the harness: fixtures that define expected nodes, relationships, evidence, and unsupported constructs, CI that reports the metrics, machine-readable AI comparisons, and reviewable fixture updates. The framework, database, correlation, diagram, and AI outputs it depends on now exist (ADR-015 to ADR-022).

The main risk of such a harness is circularity. If an expected set is copied from what the tool produced, every score is perfect and nothing is measured.

## Alternatives

- Generate golden files from tool output and review the diff. Rejected: it makes the tool its own oracle, and reviewers tend to accept what they are shown.
- Compare generated documentation text to reference documents. Rejected by ADR-011: appearance is not accuracy.
- Store IDs and hashes in golden files. Rejected: evidence identifiers embed content hashes, and Windows checkouts differ in line endings.
- A new `repodocs evaluate` command. Not done: it is a CLI-contract decision, and a module runner meets the issue (see below).

## Decision

### Golden cases
- A case is `tests/golden/<name>/golden.json` (schema `schemas/golden-case-1.0.json`). It names fixture repositories and lists expected nodes, relationships, evidence locations, reported limitations, cross-repository candidates, diagram artifacts, and optionally a recorded model response. One fact is one line, so an update is a readable diff.
- Cases are written from the fixture sources and the ADRs, not from tool output. Facts are named by semantic selectors (type, repository, name, and attributes only to disambiguate) and by file path and line. A selector matching more than one node is an error in the golden. Golden files may not contain identifiers or hashes, and a test enforces it.
- A node or relationship type that appears in a case is enumerated completely, so extra findings of that type count against precision. A case may also declare types as complete when none are expected. Limitations and candidates are complete when the case says so.
- There is no command that rewrites a golden file from output. A report lists `missing` and `unexpected` facts in the golden's own selector form. Deciding which side is wrong, and copying a line across by hand, is the review. When the tool misses a real fact, the golden keeps it and the score stays below 1.

### Metrics
Every ratio states its numerator and denominator and is `null` when the denominator is zero, never 1.0.
- **Precision and recall** for nodes, relationships, and limitations, over the complete types: matched over observed, and matched over expected. A relationship with the wrong classification counts as both missing and unexpected.
- **Evidence recall**: expected evidence assertions satisfied. A line assertion requires span evidence containing that line.
- **Limitation matching** is by repository, kind, and location, never message text. A limitation's evidence must contain the text that makes it a limitation.
- **Cross-repository accuracy**: matched over the union of expected and observed candidates, so missing and extra candidates both lower it. It is measured against the expected candidate set, including the competing and the non-matching ones; it is not a measure of real links. The report also counts forbidden pairs present, confidence mismatches, candidates that are not inferred, and observed relationships between repositories, all of which must be zero.
- **Provenance resolution**: every evidence record is checked against the files on disk: the file exists, its hash is recomputed, the span lies inside it, and the revision equals the snapshot's. Every evidence reference elsewhere in the IR must resolve to a record.
- **Unsupported-claim rate**: deterministic claims that cite no evidence or evidence that does not exist, over all deterministic claims.
- **Diagram validity**: each generated `.mmd` file is checked with RepoDocs' built-in Python validator. This is not a Mermaid renderer. An ERD case also checks entity and relationship counts derived by hand from the SQL.
- **Unchanged-scan stability**: the pipeline runs twice, in one interpreter, on copies in different directories; the IR hash and every generated artifact must be identical. That cannot reveal ordering that depends on the hash seed, so a test also runs the whole evaluation in separate processes with different `PYTHONHASHSEED` values and requires identical reports.
- **AI comparison**: a reviewed recorded response is replayed through the real claim validation. The report gives the existing contribution metrics and a deterministic-versus-AI comparison of the unsupported-claim rate, counting accepted claims that fail validation or that the review labels unsupported. The delta must not be positive. It says nothing about the quality of any model.

### Report and gate
- One versioned JSON report (`schemas/golden-report-1.0.json`) contains no hashes, identifiers, or machine paths, so it is identical on every operating system.
- The committed `tests/golden/baseline-report.json` holds exact values. A change in either direction fails a test and shows as a diff, so an improvement is reviewed too. Failures are separate invariants: unresolved provenance, unsupported claims, invalid or missing diagrams, unstable output, authoritative candidates, forbidden candidates, and an AI comparison that increases the unsupported rate. Recall below 1 is tracked in the baseline, not a failure.
- The gate is pytest, so the four required check names are unchanged. The `Tests` workflow also prints the table to each job's step summary.
- Tests prove the harness can fail: each metric is moved by damaging a copy of a real result, and sabotaging the harness itself fails the suite.

### Runner
`python -m repodocs.golden <cases> [--baseline FILE] [--output FILE] [--summary]` exits 0 on success, 1 when an invariant fails or the baseline differs, and 2 for an unreadable or empty case set. It adds no CLI command and no exit code to `repodocs`.

### Awaiting owner approval
- The runner is a module, not a `repodocs evaluate` command. A command would let users evaluate their own golden fixtures but is a CLI-contract change.
- **An ADR-017 wording conflict.** ADR-017 says partitioned parents and `PARTITION OF` statements are reported as limitations "instead of being applied". The analyzer, its own tests (`partition_of`, "only the declared columns are recorded"), and the fixture README all treat partitions as tables and report the partitioned parent as a limitation. The case follows the implementation. Either the ADR sentence or the behavior should change; that is a decision for the owner.
- Modeling judgments recorded in the case files: an ALTER of an undefined table is `ambiguous-reference`, a functional route is anchored at the method declaration, and a functional route and a constant route are both `dynamic-routing`.

## Findings recorded by the first run

The first comparison disagreed with the golden files in seven places. Five were golden errors and were corrected with the reasoning in the case files: three omissions (the `SmartCartApplication` class, a YAML flow collection, a second reflective call), one over-specific line (the functional route, which ADR-015 anchors at the declaration because bodies are not scanned), and the partition handling above. Two were real analyzer findings and were kept as misses: the Spring adapter cites the line after an `@Value` SpEL annotation and the line before an `@Profile` annotation, so the cited evidence does not contain the text that triggers the limitation. Limitation recall for that case is 0.8 until the analyzer is fixed.

The agreement of the correlation case is weak evidence of independence, because its node names and candidates had been seen before that case was written. The Spring and Postgres disagreements are the evidence that goldens were not copied from output.

## Consequences

The standalone React fixture, the ORM-to-table links, constructor-injection edges, and Spring entity associations have no golden case yet, so the report says nothing about them. Scores describe fixtures that were written by the same project, and a perfect score would not show the tool works on unfamiliar repositories. The built-in diagram validator can accept source that a Mermaid renderer rejects. The baseline needs a reviewed regeneration whenever a metric legitimately changes.
