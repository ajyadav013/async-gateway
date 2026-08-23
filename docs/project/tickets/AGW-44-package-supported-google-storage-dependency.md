# AGW-44: Package the supported Google Storage dependency

- **Status:** IN REVIEW
- **Branch:** `codex/gcs-selector`
- **Story:** GCS-01 — [GCS selector story plan](../../specs/gcs_selector_stories.md)
- **Spec:** [GCS selector specification](../../specs/gcs_selector_spec.md)
- **Decisions:** Bounded GCS decisions of record in [the GCS selector specification](../../specs/gcs_selector_spec.md); no ADR is required.
- **Acceptance criteria:** AC1.4, AC12.1, AC12.3, AC12.4; security findings DEP-V-001, DEP-V-002, SEC-S-001
- **Files (declared scope):** `pyproject.toml`, `.gitignore`, `.github/workflows/ci.yml`, `.github/workflows/publish.yml`, `tests/test_packaging.py`, `docs/project/tickets/AGW-44-package-supported-google-storage-dependency.md`, `docs/project/tickets/index.json`

## Why

The selector cannot be imported reliably, or distributed safely, without the
frozen `google-cloud-storage>=3,<4` package contract (GCS-01; AC1.4,
AC12.1, AC12.3, AC12.4). Security Cycle 2 also requires the development and
build setuptools paths to share a fixed-safe constraint and requires narrow
local environment/key exclusions before the selector can clear release gates.

## Historical GCS-01 RED-first plan and definition of done (completed)

This original two-file plan records the completed GCS-01 dependency slice and
does not describe the active Security Cycle 2 defect lane.

- **RED first:** Add focused packaging assertions that fail until runtime
  metadata contains exactly `google-cloud-storage>=3,<4` and the clean
  package/import inventory recognizes the selector dependency; run the focused
  packaging test and capture the failure.
- **GREEN/refactor:** Add only the approved metadata entry; keep no emulator or
  extra dependency. Re-run the focused packaging test, then the relevant
  install/import/build checks prescribed by the specification.
- **Done checks:** Exact dependency range present; no new dependency; focused
  packaging assertion green; file count remains 2.

## Active Security Cycle 2 RED-first plan and definition of done

- **RED first:** Eight focused rows fail against the vulnerable setuptools
  resolution, legacy import-order plugin, and four missing local-secret ignore
  patterns. The setuptools contract test must reject marker-, extra-, and
  direct-URL-bearing in-memory mutations as well as unsafe version boundaries.
- **GREEN/refactor:** Use only `flake8-import-order==0.19.2` and the identical
  unconditional `setuptools>=83,<85` build/dev requirement; add only the four
  narrow ignore patterns and update the two now-stale workflow comments.
- **Done checks:** The exact seven declared files remain the complete scope;
  both parsed setuptools requirements have the exact canonical name and range,
  no marker, extras, or URL; 75/78/82 and 85 are rejected while 83/84 are
  accepted; the eight focused rows, packaging suite, lint/format, JSON,
  diff/scope, ignore, and no-`uv.lock` checks pass. Independent dependency and
  secret scanner rechecks remain the Security Clear gate.

## Work Log

- 2026-08-22 — Opened from approved GCS-01 planning before implementation;
  recorded the frozen two-file boundary, acceptance criteria, RED-first proof,
  and dependency relation. Files: this ticket and the local ticket/wiki index.
- 2026-08-22 — Implementation started in the isolated backend worktree at
  planning commit `057bc367`; loaded the approved specification, story plan,
  repository rules, and requested implementation skills before the RED step.
- 2026-08-22 — RED: `pytest tests/test_packaging.py -k 'gcs_runtime'
  -q --no-cov` failed 2 tests because the source and installed metadata had no
  `google-cloud-storage` requirement. GREEN: after adding only
  `google-cloud-storage>=3,<4` and reinstalling the editable project, the same
  command passed 2 tests with 104 deselected. Product commit: `60f5074`.
- 2026-08-23 — JOIN-2 evidence: code review Iteration 3 APPROVED at product
  HEAD `e22a440b` (C0/H0/M0/L0); build-green passed 5,927 tests with 20
  declared skips, repo-wide and changed-module 100% statement/branch coverage,
  and artifact verification; contract-clear VERIFIED (C0/H0/M0/L1), with only
  the nonblocking stale internal protocol-count docstring. Downstream tester,
  security, operability, acceptance, and PR stages remain open.
- 2026-08-23 — Reopened the package story for Security Cycle 2 findings
  DEP-V-001/DEP-V-002/SEC-S-001 and expanded its declared scope to the exact
  seven-file defect lane; no runtime/GCS behavior is in scope. Files:
  `pyproject.toml`, `.gitignore`, `.github/workflows/ci.yml`,
  `.github/workflows/publish.yml`, `tests/test_packaging.py`, this ticket, and
  `docs/project/tickets/index.json`.
- 2026-08-23 — RED: eight focused contract rows failed against
  `flake8-import-order==0.18.2`, contradictory `setuptools>=77.0.3`/`<76`
  paths, and the four missing local-secret exclusions. GREEN: the same eight
  rows passed after moving to `flake8-import-order==0.19.2`, aligning build and
  dev on `setuptools>=83,<85`, updating only stale workflow comments, and
  adding the exact four ignore patterns. Files: the seven declared files.
- 2026-08-23 — Final developer validation used a fresh source tree assembled
  from `981a022` plus only this seven-file patch and a fresh sibling Python
  3.14 environment. It resolved `flake8==7.1.0`,
  `flake8_import_order==0.19.2`, `setuptools==84.0.0`, and
  `google-cloud-storage==3.13.1`, with one canonical import-order distribution
  and one `I` entry point. Full flake8 and the exact CI format selector emitted
  no output; 5,952 tests passed with 20 declared skips and 100% coverage of
  4,892 statements plus 1,544 branches. Mypy checked 33 source files;
  suppression, compile, Bandit Medium/High, JSON/YAML, diff/scope, ignore,
  hidden-secret, and no-`uv.lock` checks passed. Isolated wheel/sdist build,
  Twine, fresh artifact installs/imports, and `pip check` passed. Files: the
  seven declared files; no production module changed in this defect lane.
- 2026-08-23 — Code review Iteration 1 resolved M1/M2 by freezing the complete
  unconditional setuptools Requirement shape for both install paths, proving
  marker/extra/URL mutations fail in memory, and separating completed GCS-01
  history from the active Security Cycle 2 seven-file DoD. The focused eight
  rows and 122 packaging tests passed with 2 declared skips; scoped and full tracked-Python lint
  plus format checks emitted no output. Files: `tests/test_packaging.py`, this
  ticket, and `docs/project/tickets/index.json`.
- 2026-08-23 — Final security and delivery evidence: commit
  `362e184f29ed42a53e3e54984027add599582531` completed the dependency and
  secret-hygiene lane; independent dependency and secret rechecks passed, and
  Security Clear reported C0/H0/M0/L0. Final Pipeline Green inherited
  unchanged dependency/package bytes and the artifact/source-install proof.
  Status is IN REVIEW on `codex/gcs-selector`; Acceptance remains pending and
  no PR is recorded. Files: this ticket and `docs/project/tickets/index.json`.
