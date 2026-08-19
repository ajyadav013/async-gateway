# AGW-31: CHANGELOG and examples

- **Status:** DONE
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

- Keep-a-Changelog with a `1.0.0` entry stating plainly ~~that the package was **never published**~~ **(CORRECTED 2026-08-18 — see the work log)** that this is a **rename with a discontinued predecessor**, that the version moved **down** from the fork-inherited 2.7.3, and why that is safe
- a **migration path** for existing `asyncio-requests` users, and a **Security advisory** for the three defects live in the published 2.7.3
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

- **`examples/` ships in neither the wheel nor the sdist** (R35-AC4, which asks for the choice to be
  made explicitly). The scripts are read in the repository, where the README links them; shipping
  them would put a second copy of the API's documentation inside every install, free to drift
  against the first. Asserted twice — by a suite test against a real build into `tmp_path`, and by
  a CI step against the artifact `build-and-install` has already built, because the suite test
  skips where `build` is absent (it is not in the dev extra).
- **The R35-AC5 line budget is measured on code, not on the file.** The module docstring is the part
  a reader most wants from an example; counting it against a ~40-line budget would buy brevity by
  deleting explanation. All five are ≤ 33 code lines.
- **FTP and SFTP are import-checked, not executed.** Both need a real daemon, and R35-AC3 forbids
  pointing an example at a service this project does not own. What they can still be held to — that
  they name loopback, parse, import, and stay inside the budget — they are.
- **The changelog CI escape is a *label*, not a commit-message token.** `skip-changelog` is visible
  on the pull request and in its history; a `[skip changelog]` in a commit body is not.

## Work Log

- **Breaking changes derived from history, then re-verified against the code.** Source was
  `git log --oneline 31542aa..HEAD` with each commit body read, cross-checked against the work logs
  on AGW-18 ("Breaking change (for the CHANGELOG — AGW-31 / S31)") and AGW-29, and against
  `git show 31542aa:` for each old default. **Two claims in the story brief did not survive that
  check and are deliberately absent from the CHANGELOG:**
  - *"`http_file_upload_config` no longer exists"* — it does. `logic/http_client.py:764` validates
    it and it is refused only when combined with a GET (L8). Writing it up as removed would have
    told a consumer to delete working configuration.
  - *"the version moved down from 2.7.3"* — true of the release, but **not yet true in this lane**:
    AGW-28 performs the reset and has not merged into this base, where `pyproject.toml:18` still
    reads `2.7.3`. The entry is written as the release statement it will be, and the section is
    headed `[1.0.0] — unreleased`.
- **Every example written against the running library, never the README** (which is stale: it names
  `ujson`, a `download_file_from_s3` signature that changed, and a SOAP client that did not exist).
  Signatures, `protocol_info` keys, envelope keys and error codes were read out of
  `async_gateway/` and then confirmed by execution before the example was written — including that
  a SOAP response body arrives as an `Element` at `protocol_details['soap_body']`, that a 404 is
  `ok=False` / `HTTP_STATUS` / 404 with `json` **still populated** (E11), and that
  `request_type='FLY'` raises `UnsupportedVerbError` synchronously while an SFTP call missing
  `mode` returns a `CONFIG`/400 envelope — the two-shaped contract the error-handling example
  exists to teach.
- **Mutation proofs.** Both load-bearing guards were checked by mutating the code they protect and
  confirming red, restoring from a `cp` backup each time:

  | Mutation | Expected | Result |
  |---|---|---|
  | `from async_gateway.async_gateway import request` → `request_renamed` in `http_example.py` | the import test and the executed test go red | **2 failed** (`test_every_example_imports_cleanly[http_example.py]`, `test_the_http_example_runs_against_a_real_server`), `ImportError` |
  | `result["json"]` → `result["tat"]` in `error_handling_example.py` — the exact rename this release made | the executed error-handling test goes red | **1 failed**, `KeyError: 'tat'` |

  A first attempt at the second mutation was a **no-op** (the anchor text sat inside an f-string and
  did not match); it reported 34 passed, which would have read as a surviving mutant. Recorded
  because the lesson is that a green run after a mutation must be confirmed to be a *mutated* run —
  `diff` against the backup, which is what caught it.
- **The changelog CI gate was executed, not merely written.** The script was run against a throwaway
  git repository over five cases: code-without-entry → **exit 1**; the same diff with a
  `skip-changelog` label → exit 0; code-with-entry → exit 0; docs-only → exit 0; and the near-miss
  label `skip-changelog-later` → **exit 1** (the `case ,$LABELS,` comma-anchoring rejects the
  substring). The job is `pull_request`-only: on a bare push there is no base to diff and no label
  to grant the escape.
- **The workflow was parsed** (`yaml.safe_load`) — six jobs, no `continue-on-error` anywhere but the
  comment asserting its absence — and the header comment listing the jobs was updated to name the
  new `changelog` job rather than going stale on its first day.
- **Shared-file discipline.** `tests/test_packaging.py` and `.github/workflows/ci.yml` are shared
  with the concurrent S28 and S23 lanes, so every edit is strictly additive: new imports, new module
  constants, and a new block appended below the existing matrix tests; new CI steps appended inside
  `lint-type-test` and `build-and-install`, and one new job. No existing test, step or job was
  modified. One import-order fix (`subprocess` before `tarfile`) was needed to hold flake8 at its
  baseline; it is inside the block this story added.
- **Verification** (venv `../venv-s31`, base `73610ab`):

  | Command | Result |
  |---|---|
  | `python -m pytest -q` | **1134 passed**, 0 failed, 0 xfailed, coverage **98.41%** (baseline 1104 / 98.41% — 30 tests added, coverage held) |
  | `python -m mypy async_gateway` | **Success: no issues found in 27 source files** |
  | `python -m flake8 async_gateway tests examples` | **19** findings, identical to the baseline for `async_gateway tests`; `flake8 examples` alone is **exit 0** |
  | `python -m compileall -q examples/` | exit **0** |

- **Left to the stories that own them:** the version reset and the PyPI-404 re-verification (AGW-28,
  OQ4); the resilience ADR this CHANGELOG cites at `docs/decisions/0001-resilience-library.md`
  (AGW-23, OQ12) — the decision's *substance* is recorded here from spec Ruling D, but the ADR file
  does not exist in this lane; the README's "Versioning policy" section linking to this file
  (AGW-29); the Sphinx file deletions themselves (AGW-28) — this CHANGELOG records the R31-AC4
  decision and its reason, which is this story's criterion, not the deletion.
- **Commits:** `e2b94fe` (examples + their tests), `5974cde` (CHANGELOG + CI).

### 2026-08-18 — corrected premise + security advisory (post-`v1.0rc-1`)

- **The false premise.** The whole release was written on "this package has never been published,
  therefore zero installed users". That was verified against the **wrong name**:
  `pypi.org/pypi/async-gateway/json` does 404, but `async-gateway` is the *new* name. This code ships
  today as [`asyncio-requests`](https://pypi.org/project/asyncio-requests/) — 12 releases from
  2022-02-24, currently `2.7.3` (2023-01-02, the exact inherited version), ~110 downloads/month, same
  `request()` entry point and same `logic/{http,ftp,sftp,soap}.py` layout.
- **What survives.** The `1.0.0` reset stands, for a corrected reason: the *distribution name*
  `async-gateway` has no history, so no resolver, pin or `>=2.0` constraint can be broken. The release
  is **a rename with a discontinued predecessor**, not a first release. No engineering changed.
- **What was added.** `CHANGELOG.md`: the rename narrative, a "For existing `asyncio-requests` users"
  migration section, and a **Security advisory** for `asyncio-requests <= 2.7.3` naming
  `logic/sftp.py:42` (`known_hosts=None` — MITM on SFTP), `logic/ftp.py:50-52` (`UnboundLocalError` on
  every call), and the zero-byte `logic/soap.py`, plus the unpatched dependency pins. `README.md`:
  a "Migrating from `asyncio-requests`" section linking the advisory, plus versioning-policy and
  attribution corrections.
- **Pinned by tests** so the docs cannot drift back: `tests/test_docs.py` (migration section, advisory
  link, no revived false claim) and `tests/test_packaging.py` (the reset's stated reason).
- **Remaining publish-time step, human-owned:** a final `asyncio-requests 2.7.4` pointing at the new
  name. Not done here.
