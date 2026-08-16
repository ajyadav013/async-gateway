# AGW-31: CHANGELOG and examples

- **Status:** OPEN
- **Story:** S31 — spec Step 30, size M (`docs/specs/v1_release_stories.md` §4, Phase 7)
- **Spec:** `docs/specs/v1_release_spec.md` — R33 all, R35 all (Group O — Value-adds) · R31-AC4 (Group N — Documentation)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** records the **R7 decision** (AGW-23's `docs/decisions/0001-resilience-library.md`) and the **R31 docs decision** (AGW-28); makes no new decision of its own
- **Files (declared scope):** +`CHANGELOG.md`, `examples/{http,ftp,sftp,soap,error_handling}_example.py` · ~`.github/workflows/ci.yml`, `tests/test_packaging.py`

## Why

R33 requires a CHANGELOG and R35 an `examples/` directory — the two artifacts a first-time consumer
reads before anything else. The `1.0.0` entry has to state plainly that the package was never
published and that the version moved **down** from the fork-inherited 2.7.3, and it has to list the
breaking changes **as breaking** (the single envelope, the removal of `api_response`, `tat`→`latency`,
the FTP `verify_ssl` default flip, SFTP host-key verification on by default, the `logic/*` renames).
R31-AC4 records the docs decision here, and R4-AC6's Development Status classifier is justified here
too. The CI gate is what stops the changelog rotting after this release.

## Definition of Done

- Keep-a-Changelog with a `1.0.0` entry stating plainly that the package was **never published**, that the version moved **down** from the fork-inherited 2.7.3, and why that is safe
- breaking changes listed **as breaking**: the single envelope, removal of `api_response`, `tat`→`latency`, the FTP `verify_ssl` default flip, SFTP host-key verification on by default, the `logic/*` renames
- records the **R7 decision** (AGW-23's ADR) and the **R31 docs decision** (AGW-28)
- the `Development Status` classifier choice justified here (R4-AC6's discharge point)
- versioning policy stated once and cross-linked with the README
- a CI step fails a PR touching `async_gateway/` without touching `CHANGELOG.md`, with a documented `skip-changelog` escape
- five examples, each **≤ ~40 lines**, one thing each, syntax-checked in CI, **no third-party or corporate endpoint**
- wheel/sdist inclusion decided explicitly and asserted by a packaging test

## Dependencies

- **blockedBy:** AGW-28, AGW-23
- **blocks:** AGW-32

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
