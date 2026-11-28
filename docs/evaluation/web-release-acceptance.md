# Web-first release acceptance evidence (#77)

This records what was verified for the local web-first baseline (epic #63) and what is still open. Green CI, replayed
providers and visible diagrams do not show that the diagrams are useful; that judgement is the #54 owner decision.

| Item | Value |
|---|---|
| Engine | `main` at `b49205584c3e7426133510854c4f20068d10e0d4` (packaged walkthrough); parity run on `4ce55076` with the same engine code |
| Platform | Windows 11, Python 3.11.9, Node 24, Chrome 158; CI also runs Linux and Windows on Python 3.11 and 3.14 |
| EventShare | `mudabs/EventShare` at `93970207969ecdb9b35cf5c1f8e811ef930a2a55` |
| #54 subjects | DigitalBoneBox `602e9c53c656f55b5e69b1a4dd4c6f79e53b368c`, Flaskr `49943b5a94413a530761350f04e896f76e8d620e` |

## Criteria and evidence

| Criterion | Evidence | Status |
|---|---|---|
| Equivalent CLI and web inputs give matching artifacts | `test_the_cli_and_the_http_adapter_produce_identical_state_and_artifacts` (replayed AI), `test_cli_and_an_in_process_adapter_produce_identical_ir_view_and_artifacts`, `test_web_artifact_bytes_match_cli_for_same_source`; EventShare deterministic run below: IR, manifest, Markdown, overview and ERD byte-identical | Met |
| Fresh-user EventShare flow without SHA, workspace or token setup | Packaged app on `b4920558`: URL alone pins `93970207`; provider chosen from the model list (`qwen2.5-coder:7b`, local); coverage disclosed before the run; one model request; viewer, evidence, refresh and reopen work. The AI result was **partial** (`no-progress`). A rehearsal on the same code and model completed (`succeeded`, 1 request, 191 s). | Ran end to end; AI result not yet complete in the live run |
| Back/Forward, refresh, deep links, browser and server restart | `check-nav-97.mjs` (Back, Forward, refresh, bookmarked result, browser restart with the same profile), `test_restart_restores_owned_draft_and_result_without_inference_and_requires_fresh_authorization`, home tests (reload, history reopen) | Met |
| Reopen, restore and download never run inference | Provider factory counts asserted in #97, #98 and #99 browser tests (0 for local runs, exactly 1 for one consented AI run, still 1 after reload, reopen, reconnect and cancel) | Met |
| Expired provider, revoked consent need explicit recovery | After restart the provider reads 404 and old consent is refused 403; home asks to connect again after a reload; a second AI run shows a fresh disclosure review | Met |
| SQLite migrations, ownership, cleanup, reconciliation; CLI and Actions need no database | `tests/test_sqlite_profile_history_95.py` (migrations, corrupt and locked store, interrupted writes, file and metadata mismatch, cleanup of saved work), draft ownership 404, CLI suites run without the web extra | Met |
| Dirty and changed sources, stale views, no-op, manual edits | #54 deterministic benchmark (dirty working tree regenerated, committed change pinned and regenerated, second generate a no-op, manual edit refused with exit 4 and kept); changed source gives 409 `stale-preview` in the web flow | Met |
| Provider failure, cancellation, exhausted budget | Home tests (provider failure in plain language, cancel during a held run), `test_browser_architecture_canary_and_partial_budget`, #94 later-round admission tests | Met with replayed providers |
| Exact source provenance, explicit consent, uncertainty labels, generated-file ownership | Pinned revisions shown in history and the viewer; consent bound to provider, host, model, policy and preview hash; "not current" notices; ownership refusal (exit 4) | Met |
| #54 real-model outcomes and owner go/no-go | See #54 and `docs/evaluation/architecture-discovery/REPORT.md` | Open |
| Intended-user usability at normal zoom with real diagrams | Owner walkthrough on the running app | Open (owner verification) |

## EventShare CLI and web parity (deterministic)

Run on the integrated #99 head before merge (same engine code as `main` at `4ce55076`), EventShare as an allowlisted local
source at the pinned commit. The web job ran first; the CLI then ran `scan`, `generate` and `validate` in the same
workspace after its outputs were removed (the IR records absolute source paths, so both must use the same root).

- Web job: `succeeded` in 5.36 s; CLI exit codes: scan 0, generate 0, validate 0 (7.0 s).
- Downloaded artifacts match the files on disk: yes. Identical overall: yes.

| File | Web (SHA-256 prefix) | CLI | Equal |
|---|---|---|---|
| `artifact-manifest.json` | `707fa888d7243f1a` | `707fa888d7243f1a` | yes |
| `docs/architecture.md` | `f350eb82d379842e` | `f350eb82d379842e` | yes |
| `docs/architecture.mmd` | `16b8b9452f10cd03` | `16b8b9452f10cd03` | yes |
| `docs/erd.mmd` | `c37c6d2844a09720` | `c37c6d2844a09720` | yes |
| `ir.json` | `07210a8856aa5c34` | `07210a8856aa5c34` | yes |

## Known limitations found during verification

- Evidence hashes depend on checkout line endings (CRLF versus LF), so a Windows checkout and a Linux runner can disagree about whether docs changed. Recorded in #54; needs its own issue.
- The deterministic EventShare overview is a single-level fan-out of 15 nodes; Fit makes labels small, so the viewer starts at a readable 60 % and needs panning.
- Signing out hides saved projects from new sessions (ADR-040); the owner chose to keep this and document it.
- A context-window refusal at submission is reported as `invalid-request` (422); only the preview carries `context-window` as a code.
- The external-provider run (OpenAI `gpt-6 Sol`) has not been performed: no API key is configured and no spend cap or price profile was given.
- The first EventShare preflight failure (92,643 byte-bound input tokens against a 32,768-token window) is fixed by #112: the context is fitted to the model's input allowance and disclosed. With the default 16,000-token output reserve only 4 of 230 candidate files fit and the storage layer is not covered. The byte bound is conservative: the fitted request measured 15,371 byte-bound tokens but the model reported 3,210 input tokens.
- During a run the progress card claimed no model request was made while one was in flight; fixed by #114.

## Live AI walkthrough (packaged app, `b4920558`)

- Wheel SHA-256 `7766ed6be9b3281025898a4909475777eff4c18e23d2105260aef6f06284250c`, fresh virtual environment, `repodocs-web --context-window-tokens 32768 --model ollama:qwen2.5-coder:7b --model openai:gpt-6-sol`; `/health` ok, `/ready` with `ui_built: true`.
- Home page: `https://github.com/mudabs/EventShare`, AI architecture. Provider setup listed `qwen2.5-coder:7b` (Ollama) and `gpt-6-sol` (OpenAI) from the operator's list; choosing one filled the model field.
- Before the run: "4 of 230 candidate source files are included. The context was reduced to 16,812 bytes to fit the model's input allowance. Not covered: storage files."
- Result: **partial** (`no-progress`) after one request (4,019 tokens), with 4 components (1 supported, 3 unverified: generic names `api`, `backend-service`, `db`) and 1 connection rejected as unsupported. The supported component cites `frontend/src/components/Gallery.tsx:164-207` at `93970207`.
- Refreshing `/viewer/<job>` and reopening from Saved projects made only GET requests: no job, provider or model call.
- This is not a successful quality outcome. It is evidence for the #54 decision, not a substitute for it.

## Packaged build

- Clean clone of `main` at `4ce55076`, `python scripts/build_web_package.py`, `pip wheel`: `repodocs-0.1.0-py3-none-any.whl`, SHA-256 `cbf45ceed33c1580d9a832709c9424fcd72a530655d28a16d17e51d1d15ff097`.
- Installed with `[web]` in a fresh virtual environment (not editable): `repodocs init/add/scan/generate/validate` all exit 0 on the express fixture with no server or database; `repodocs-web` serves `/health` `ok` and `/ready` with `ui_built: true`.
- Browser walkthrough on that instance: EventShare by GitHub URL in local-facts mode (pinned `93970207`), overview and ERD readable, saved project reopened without inference. It found one defect, fixed in #110: saved GitHub projects showed `undefined/undefined` instead of the repository and revision.
