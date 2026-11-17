# Architecture discovery evaluation: DigitalBoneBox and Flaskr

Epic #47, issue #54. Date: 2026-11-17.

## Status in one paragraph

The evaluation workflow, the scoring harness, the expected sets, and a reproducible demonstration on two real repositories are finished. **No live model was run**, so the question the issue asks (does AI-assisted discovery give users the architecture understanding they expected?) is **not answered**. The only AI-assisted numbers below come from a scripted reference replay that answers from the expected set. They test the pipeline and say nothing about model quality. Deterministic mode, measured for real, produces no application components for either repository. The owner go/no-go decision is pending and is not made here.

## Subjects

| | DigitalBoneBox | Flaskr (Flask tutorial application) |
|---|---|---|
| Repository | https://github.com/oss-slu/DigitalBoneBox | https://github.com/pallets/flask, path `examples/tutorial` |
| Revision | `602e9c53c656f55b5e69b1a4dd4c6f79e53b368c` (2026-11-09) | `49943b5a94413a530761350f04e896f76e8d620e` (2026-10-30) |
| Licence | MIT | BSD-3-Clause |
| Stack | Node.js and Express API, vanilla JavaScript and HTML client, JSON data files, Python extraction scripts | Python and Flask, SQLite, Jinja templates |
| Size | 798 scanned files, 965 tracked | 22 scanned files |
| Access | Public, cloned read-only into a scratch directory; no code was run | Same |

Why Flaskr: it is small and clearly licensed, and it contrasts on every axis that matters here: a different language, server-rendered pages instead of a client that calls an API, a relational database instead of JSON files and an optional Redis store, and blueprints instead of routers. It is also a case where RepoDocs has no analyzer, as for DigitalBoneBox's Express code, so deterministic mode has nothing semantic to say about either. A Java or React project would have shown the existing analyzers and was avoided for that reason.

## Method

1. The expected components and relationships were written by reading the source, before any output was scored ([`expected/digitalbonebox.json`](expected/digitalbonebox.json), [`expected/flaskr.json`](expected/flaskr.json)). Each entry names the files and lines inspected. Deployment-only items (the Vercel entry) are marked and scored separately, because the epic asks for deployment to be kept apart from application dependencies. Each file also lists relationships the source does **not** establish, so an assertion of them can be called unsupported.
2. Components are matched to the expected set by the files they claim, not by name. A connection counts as supported when both ends map to expected components and the expected set has that relationship with that kind.
3. The harness (`python -m repodocs.architecture.evaluation`) reports component and connection precision and recall, citation resolution against the snapshot, member paths that exist, inferred versus proposed counts, evidence items with a commit link, what the deterministic scan found, and a review list with an empty verdict for every connection. A match score does not show that a relationship is true; the review list is for a person to open each cited location and decide.
4. Competitor diagrams were not used as ground truth.

## What was measured for real

Deterministic mode, `repodocs scan` and `generate` on each repository:

| | DigitalBoneBox | Flaskr |
|---|---|---|
| Snapshot | commit `602e9c53c656`, clean | commit `49943b5a9441`, clean |
| Nodes | project 1, repository 1, language 7, manifest 6, dependency 18 | project 1, repository 1, language 5, manifest 1, dependency 1 |
| Limitations reported | 0 | 0 |
| Application components | **0** | **0** |
| Generated files | `architecture.md`, `architecture.mmd` (a repository and language inventory) | same |

This is the honest depth of deterministic mode for an unsupported stack: an inventory, with no statement of what the application is or how it connects. It needs no key or network and is unchanged.

Workflow and safety behavior, run with the reference replay in place of a model (a separate documentation repository is the destination; raw numbers are in [`reference-replay-run.json`](reference-replay-run.json)):

| Check | DigitalBoneBox | Flaskr |
|---|---|---|
| Context preview (nothing sent) | 8 excerpts, 18,723 bytes, 0 redactions, 3 files denied by name | 3 excerpts, 2,936 bytes, 0 redactions, 1 file denied by name |
| Stored view validates and renders | complete, 6 components, 5 connections | complete, 5 components, 8 connections |
| `validate` | exit 0 | exit 0 |
| Second `generate` | no change (no-op) | no change (no-op) |
| `discover --refresh` with the same replay | byte-identical stored view | byte-identical stored view |
| Real Mermaid renderer on the diagram | rendered | rendered |
| HTML page in Chrome | drew, zoomed, 3 scripts, no CSP violation, no request, no dialog, no console error | same |
| Page size | 3.6 MB (it embeds the Mermaid bundle) | 3.6 MB |
| Time (scan, discover, generate) | 0.8 s, 0.2 s, 0.3 s | 0.3 s, 0.03 s, 0.3 s |
| Links | 17 of 17 evidence items link to the exact commit; two sampled links resolved through the GitHub contents API | 22 of 22 link to the exact commit |

The times are for the replay and the local pipeline. They say nothing about model latency.

Source change, dirty tree, and ownership on Flaskr:
- **Dirty working tree.** After an uncommitted edit to `flaskr/blog.py` and a rescan, the snapshot is `content`, the earlier view is stale, its architecture files are retired, and `generate` warns. Discovery on the dirty tree stores a view whose six evidence items have no commit link, and the map says "Working-tree content, not a commit: no source link is offered".
- **Committed change.** A committed edit to `flaskr/db.py` in a throwaway clone made the stored view stale and removed its files with the same warning.
- **Ownership.** Appending a line to the generated `architecture-view.mmd` stopped `generate` with exit code 4 and left the edit in place.
- **Revert.** Reverting the edit and rescanning left the view stale again, because the clean snapshot is a different snapshot from the dirty one; a new discovery is needed. That is the intended contract.

## Findings from this evaluation, fixed in this PR

1. **The context inventory was crowded out by data files.** DigitalBoneBox has hundreds of data files under `boneset-api/data`. The 500-path inventory filled with them and cut off the source directories (`templates/js`, `boneset-api`), so a model could not even see them. The inventory now lists at most 12 files per directory and says how many it left out, and it covers the whole repository (177 entries, not truncated).
2. **The first round of excerpts was spent on container files.** Of eight slots, three held Dockerfiles and a compose file. The first round now gives each repository a fair share, takes README, then at most two language manifests, then entry points, and puts container files last.
3. **The page was unreadable in dark mode.** Found by looking at the screenshot: dark blue links on a dark panel, and edge labels on a dark ground. The drawing area now stays light and the links are readable (screenshots in [`screenshots/`](screenshots)).

Each has a regression test.

## Known failures and limits that remain

- **Large files cannot be read.** `templates/js/scenes.js` is 123 KB, above the 64 KiB per-file budget, so it is refused as `too-large`. It holds the client's call to the scenes API (line 21), which a model therefore cannot cite. `boneset-api/scenes.js` (44 KB) fits. Reading only the head of a large file is possible and would be a budget decision for the owner (O13).
- **The diagram can be crowded.** At six components the edges already cross and edge labels overlap (see the screenshots). This is a readability problem that a live view of a larger system would make worse; crowded views group by kind, but that has been tried only on synthetic input.
- **Edges are clickable only through the side panel**, not on the drawing.
- **Publication from a clean CI clone removes the view's files**, because the stored view lives in `.repodocs/`. The pull request says so.
- **The page embeds a 3.6 MB library.**
- **Only github.com links** are generated. Only Chrome was tried.
- **The deployment component is shown as an application component** by the reference replay, as the harness expects, and is flagged; a live model is likely to do the same, and nothing yet teaches it not to.

## What the scripted reference replay is, and is not

It answers from the expected set, so its scores (component recall 1.0, connection precision 1.0, citations resolving 1.0, no unsupported connections) are by construction. They show that the harness scores a faithful answer fully and that the pipeline carries it end to end. They are not evidence of usefulness, accuracy, or readability. The harness was also shown to notice injected faults, by tests: a missing component lowers recall and is listed, a fabricated citation is rejected and counted, an unsupported connection and a reversed one are both reported as contradicted.

## Missing evidence (acceptance criteria left unchecked)

- **Live inference.** Ollama 0.40.16 is installed on this machine with no model, and a model was not downloaded without consent. No provider, model identity, context budget, latency, or cost was recorded for a live run. To close this, run discovery with a configured model and fill the same table. The optional development baseline named for this epic is Qwen3 4B Instruct through Ollama, which needs a multi-gigabyte download and adequate memory.
- **Measured quality of model output**: useful coverage, unsupported connections, and evidence correctness for a real model.
- **Human review verdicts.** The harness writes the review list; nobody has opened each cited location for a model's relationships.
- **Readability and usefulness judgments** by the intended users.
- **Redaction review of a live run.** The context for both repositories contained no secret-shaped strings (0 redactions), and `.env`-style names were denied, but that covers the first-round excerpts only, not what a model would request.

## Recommendation (not a decision)

- **Do not call the experiment useful or production-ready yet.** The pipeline, safety, provenance, and reproducibility parts work and are tested; the usefulness question needs a live run.
- **Narrow next step:** one live run on these two pinned repositories with a local model, recording the table above, followed by a human review of every connection in the review list. If a local model cannot produce supported components for DigitalBoneBox, try one external model with the separate source consent and compare.
- **Decide before expanding:** the large-file budget, whether the stored view should be committed so CI publication keeps its files, and whether deployment components should be a separate kind.
- **Not recommended now:** more analyzers, a viewer with more features, or other providers.

The go or no-go decision is the owner's.

## Reproduce

```bash
git clone https://github.com/oss-slu/DigitalBoneBox.git && git -C DigitalBoneBox checkout 602e9c53c656f55b5e69b1a4dd4c6f79e53b368c
git clone https://github.com/pallets/flask.git && git -C flask checkout 49943b5a94413a530761350f04e896f76e8d620e
mkdir eval-dbb docs-dbb && git -C docs-dbb init -b main && (cd docs-dbb && echo docs > README.md && git add -A && git commit -m init)
repodocs --root eval-dbb init eval
repodocs --root eval-dbb add DigitalBoneBox --id digitalbonebox
repodocs --root eval-dbb add docs-dbb --id docs --role destination
repodocs --root eval-dbb scan && repodocs --root eval-dbb generate        # deterministic only
repodocs --root eval-dbb ai-configure --provider ollama --model <model>   # a local model you have pulled
repodocs --root eval-dbb architecture context                             # review what a model would see
repodocs --root eval-dbb architecture discover
repodocs --root eval-dbb generate && repodocs --root eval-dbb validate
python -m repodocs.architecture.evaluation --root eval-dbb --expected docs/evaluation/architecture-discovery/expected/digitalbonebox.json --summary
```

For Flaskr, register `flask/examples/tutorial` and use `expected/flaskr.json`. For the optional page and the real renderer, run `npm ci` in `tools/mermaid` and set `"architecture": {"html_export": true}`.
