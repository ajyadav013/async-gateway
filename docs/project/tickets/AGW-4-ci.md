# AGW-4: CI

- **Status:** OPEN
- **Story:** S4 — spec Step 3, size S (`docs/specs/v1_release_stories.md` §4, Phase 0)
- **Spec:** `docs/specs/v1_release_spec.md` — R1-AC1…9,11 (Group A), R28-AC7 (job) (Group M)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** none — reads the human answer to **OQ3** (matrix ceiling)
- **Files (declared scope):** +`.github/workflows/ci.yml`, `tests/test_packaging.py`

## Why

R1 requires continuous integration that builds, installs and imports the *real* artifact — the defect
class this release exists to close is precisely the one a CI that never installs the built package
cannot see. R28-AC7 adds the coverage-ratchet job that keeps `fail_under` monotone across the whole
release.

Closes findings: H21.

## Definition of Done

- build → clean-venv **wheel** install → submodule import → same again from **sdist** (`--no-binary :all:`), wheel cache disabled
- named steps `flake8` (baseline file) · `mypy` (still `ignore_errors`) · `pytest` · `bandit -r async_gateway -ll` · `twine check`
- **matrix `3.10–3.14` committed, not derived**, + a test that it is ≥ the floor and **contiguous**, + a **scheduled monthly job** failing when a newer stable minor exists
- **shuffled-order job** over five committed seeds
- **ratchet job** (head ≥ base `fail_under`)
- CI stands up on the **AGW-2 set** — never green on the un-upgraded one
- work-log evidence: scratch branches re-adding `ujson`, lowering `fail_under`, and deleting `3.14` each turn a job red

## Dependencies

- **blockedBy:** AGW-3
- **blocks:** AGW-5

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
