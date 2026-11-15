# Release Notes

This document records what every RepoDocs release must carry. It is intentionally short: no release has been cut yet, and the release process itself is not defined.

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

RepoDocs currently has no runtime dependencies, so `NOTICE` names only the project itself. If a dependency with its own NOTICE obligations is added, append its required attribution to `NOTICE` in the same change.

## Release steps

Not yet defined. No version has been published, there is no CI workflow, and no package index or GitHub Release process has been agreed. Until a release process is recorded here, the only requirement is that `main` carries the files listed above before the first public release.
