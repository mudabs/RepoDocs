# Release Notes

This document records what every RepoDocs release must carry and the repeatable procedure for cutting one. No release has been published yet.

## License

RepoDocs is licensed under the Apache License, Version 2.0 (SPDX identifier `Apache-2.0`). The decision is recorded as D28 in `docs/DECISIONS.md` and in `docs/adr/ADR-013-open-source-license.md`.

The license is declared in these places, and they must stay consistent:

| Location | Content |
|---|---|
| `LICENSE` | The complete, verbatim Apache License 2.0 text, including the appendix on applying the license. |
| `NOTICE` | The project name and the copyright holder, `RepoDocs contributors`, in standard Apache NOTICE form. |
| `pyproject.toml` | `license = "Apache-2.0"` and `license-files = ["LICENSE", "NOTICE"]`, so both files ship inside built distributions. |
| `README.md` | A License section pointing at the files above. |

Redistributions of RepoDocs, in source or binary form, must keep `LICENSE` and `NOTICE` as the license requires (section 4 of `LICENSE`).

Source files do not carry per-file license headers. Adding them is allowed but not required by the license, and is not planned.

## Third-party notices

The core CLI has no runtime dependencies. The optional `web` extra installs FastAPI and Uvicorn, and the built browser UI bundles React and Mermaid (both MIT licensed, no NOTICE obligations), so `NOTICE` names only the project itself. If a dependency with its own NOTICE obligations is added, append its required attribution to `NOTICE` in the same change.

## Release steps

The procedure below is repeatable and produces a locally verified package. It stops before anything is published:
uploading to a package index, pushing a tag or creating a GitHub Release each needs separate owner approval.

### 1. Decide what is in the release
- Every issue meant for the release is closed by a merged PR, and its project status is Done.
- Open quality gates are named in the release notes rather than hidden. For the first release that includes the
  #54 owner go/no-go and #77 web/CLI parity. Do not call the release production-ready while either is open.
- `main` carries `LICENSE`, `NOTICE` and the `pyproject.toml` license fields listed above.

### 2. Prepare the version
- Create a branch, set `version` in `pyproject.toml` (semantic versioning; the first release is `0.1.0`), and add a
  section to the release notes: what changed, known limitations, upgrade notes (for example the web store schema).
- Open a PR; the required `Tests` checks (Linux and Windows, Python 3.11 and 3.14) and `frontend-build` must pass.
  Merge it, then use that merge commit for everything below.

### 3. Build from a clean checkout of the merge commit
```
git clone https://github.com/mudabs/RepoDocs repodocs-release
cd repodocs-release
git checkout <merge-commit-sha>
python scripts/build_web_package.py
python -m pip wheel --no-deps -w dist .
```
`build_web_package.py` runs `npm ci` and `npm run build` in `frontend/` and stages the result in
`src/repodocs/web/static`, so the wheel serves the UI without Node.js. Record the SHA-256 of the wheel.

### 4. Verify the wheel in a fresh environment
```
python -m venv verify-venv
verify-venv/Scripts/python -m pip install "dist/repodocs-<version>-py3-none-any.whl[web]"
verify-venv/Scripts/repodocs --help
verify-venv/Scripts/repodocs-web --store-root <empty-dir> --port 8765
```
- `GET /health` returns `{"status": "ok"}` and `GET /ready` returns `"ui_built": true`.
- In a browser, `/` shows the home page; a local-facts run on an allowlisted source completes, the viewer shows the
  diagram, and reopening it from Saved projects makes no model request.
- Run the CLI flow from `README.md` on `tests/fixtures/express-shop-repo` copied to a temporary directory:
  `init`, `add`, `scan`, `generate`, `validate`. It needs no database, server or credential.
- Stop the server; restarting it on the same store lists the saved project again.

### 5. Tag locally and hand over for publication
- `git tag -a v<version> <merge-commit-sha> -m "RepoDocs <version>"`. Do not push the tag until publication is
  approved.
- Publication, once approved: push the tag, create a GitHub Release with the wheel, its SHA-256 and the release
  notes, and (separately) upload to a package index. None of these steps is automated yet.
