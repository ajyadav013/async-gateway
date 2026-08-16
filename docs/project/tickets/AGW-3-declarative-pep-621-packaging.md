# AGW-3: Declarative PEP 621 packaging

- **Status:** IN REVIEW
- **Story:** S3 — spec Step 2, size M (`docs/specs/v1_release_stories.md` §4, Phase 0)
- **Spec:** `docs/specs/v1_release_spec.md` — R4-AC1,2,3,4,6,7,8 (Group A — Scaffolding and quality-gate infrastructure)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; File structure — current vs target
- **Decisions:** none — reads the human answer to **OQ3** (`requires-python` floor)
- **Files (declared scope):** +`pyproject.toml`, `MANIFEST.in`, `.flake8`(if needed) · −`setup.py`, `setup.cfg`, `requirements.txt`, `requirements-dev.txt`

## Why

R4 requires PEP 621 packaging that builds a correct sdist and wheel — the package is currently not
installable-and-importable from a clean build, which is the precondition for every CI job and for the
first publish. The dependency table transcribed here is AGW-2's already-upgraded set, written once.

Closes findings: M22, M23, H26, L16, L17.

## Definition of Done

- `[build-system]`+`[project]` complete
- **the dependency table transcribed is the AGW-2 set** — written once, never as old pins a later step amends
- `requires-python=">=3.10"` (OQ3)
- AGW-1's coverage config moves from `setup.cfg` at **the same value** (the move must not lower the ratchet)
- `python -m build` → both artifacts; `twine check` passes; sdist contains README/LICENSE/pyproject; `pip install --no-binary :all:` succeeds in a clean venv
- `unzip -l dist/*.whl` shows no `tests`/`docs`/`local_development`
- **`py.typed` NOT added** (FI-13)
- no `open()` without `encoding=` in the build path

## Dependencies

- **blockedBy:** AGW-2
- **blocks:** AGW-4

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
