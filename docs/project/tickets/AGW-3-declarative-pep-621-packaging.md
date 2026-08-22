# AGW-3: Declarative PEP 621 packaging

- **Status:** DONE
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

- **PEP 639 licence form** — `license = "MIT"` + `license-files = ["LICENSE"]` with
  `setuptools>=77.0.3`; the `License :: OSI Approved :: MIT License` **classifier is dropped**, because
  PEP 639 forbids an SPDX expression and a `License ::` classifier together. Wheel METADATA confirms
  `License-Expression: MIT` + `License-File: LICENSE` (metadata 2.4). **Consequence routed to AGW-28:**
  R34's criterion "the `license` field, classifier and file agree" is now *unsatisfiable as worded* —
  it needs a spec amendment, NOT a re-added classifier.
- **`Development Status :: 4 - Beta`** (was `5 - Production/Stable`, which was L17's false claim). ~~The
  package has never been published.~~ **CORRECTED 2026-08-18:** the code is published as
  `asyncio-requests 2.7.3`; only the new name is unpublished. Beta still holds — the inherited tree did
  not import from a clean install, and this rewrite has no field experience under this name.
  R4-AC6's justification belongs in the CHANGELOG at AGW-31.
- **`download_url` not carried forward.** PEP 621 has no such field, and the old value pointed at *a
  different project's* release tarball (H25). AGW-28/R5-AC5 is therefore already satisfied.
- **`requires-python = ">=3.10"`** — OQ3 defaults to the spec's recommendation; the orchestrator ruled
  it stands.
- **Orchestrator carried forward:** `[tool.bandit]` in `pyproject.toml` is **not auto-discovered** —
  bandit reads only `.bandit` files or `-c`. **AGW-4's CI step must pass `-c pyproject.toml`**, or the
  scanner runs unconfigured.

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`c5750a8`](https://github.com/ajyadav013/asyncio-gateway/commit/c5750a8); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


**2026-08-16 — implemented, reviewed (1 Medium found and fixed), validated, committed `c5750a8`.**

*Change:* +`pyproject.toml`, `MANIFEST.in`, `.flake8`; −`setup.py`, `setup.cfg`, `requirements.txt`,
`requirements-dev.txt`. Boundary exact.

### The load-bearing check: nothing lost when `setup.cfg` was deleted

It held **five** config blocks. All five verified migrated faithfully against `git show HEAD:setup.cfg`:
`[bandit]`→`pyproject.toml` · `[flake8]`→`.flake8` (verbatim; flake8 cannot read `pyproject.toml`) ·
`[mypy]`→`pyproject.toml` with `ignore_errors = True` **still live** (proved: `--config-file=/dev/null`
surfaces 44 errors, with config 0) · `[tool:pytest]` · `[coverage:report]` with
**`fail_under = 0` unchanged** — the ratchet moved at exactly its measured value, neither lowered nor
silently raised.

### Code review — CHANGES REQUESTED → fixed → APPROVED

**Medium (fixed):** the dev extra declared bare `bandit`, but `[tool.bandit]` is TOML and bandit parses
TOML via stdlib `tomllib` (**3.11+**) or `tomli` from the `[toml]` extra. With
`requires-python = ">=3.10"` and a 3.10 leg in AGW-4's matrix, `bandit -c pyproject.toml` would have
failed to load config there — *a silently unconfigured scanner in CI, worse than none*. Changed to
`bandit[toml]`. The orchestrator verified the mechanism independently from bandit's own source
(`import tomllib` / `except: import tomli as tomllib` / "reinstall with toml extra") and its dist
metadata (`Requires-Dist: tomli>=1.1.0; python_version < "3.11" and extra == "toml"`).
**Caveat recorded honestly: no 3.10 interpreter exists on this machine (3.11/3.12/3.14 only), so the
failing 3.10 leg was never executed** — the fix rests on that declared marker, not on a 3.10 run.
AGW-4 should confirm on the real matrix.

**Low (fixed):** `Topic :: Software Development :: Build Tools` was untruthful for a request-gateway
library, and R4-AC6 requires truthful classifiers → replaced with `Topic :: Internet :: WWW/HTTP` and
`Topic :: Software Development :: Libraries :: Python Modules`.

*Lows carried forward:* `MANIFEST.in`'s comment claims the prunes keep **the wheel** lean — `prune`
affects the sdist only; wheel exclusion is `[tool.setuptools.packages.find]`. Build requires
`setuptools>=77.0.3` while the dev extra pins `setuptools<76`, harmless under build isolation but
`python -m build --no-isolation` in a dev venv will fail → **note for AGW-4**.

### Evidence

`python -m build` → both artifacts · `twine check` → PASSED ×2 · sdist carries README/LICENSE/
pyproject.toml · wheel = 27 entries, **no** `tests`/`docs`/`local_development` · clean-venv
`pip install --no-binary :all:` exit 0 · **`py.typed` absent (FI-13)** · version still `2.7.3`
(AGW-28 owns the reset). Orchestrator's own re-run: `configfile: pyproject.toml`, **3 passed**.

*Commit:* `c5750a8`.
