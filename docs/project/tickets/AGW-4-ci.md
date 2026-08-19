# AGW-4: CI

- **Status:** IN PROGRESS
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

- **The matrix ceiling is pinned against the `pyproject.toml` version classifiers.** The first draft
  had only the contiguity assertion, and the scratch run proved it insufficient: deleting `3.14`
  leaves `['3.10','3.11','3.12','3.13']`, which is *still* a contiguous ascending run, and the test
  passed. Contiguity catches a **skipped** minor; it cannot catch a **deleted highest** one. The
  ceiling therefore needs a second, independent list of the same versions, and `pyproject.toml`
  already carries one — the `Programming Language :: Python :: X.Y` classifiers, which the spec
  requires to track the matrix (`pyproject.toml` comment at the classifier block). Both mutations now
  turn a job red; see Evidence A and A2.
- **Both anti-drift files are read as *text*, not parsed.** `pyproject.toml` cannot go through
  `tomllib` because the suite runs on the 3.10 leg of the matrix it is checking and `tomllib` arrived
  in 3.11; the workflow cannot go through `yaml` because PyYAML is not a project dependency and
  adding one to satisfy a test is a poor trade (and `pyproject.toml` is outside this story's
  boundary). Both regexes are anchored, and both helpers assert on no-match, so a formatting change
  breaks the tests loudly rather than silently matching nothing.
- **Five committed seeds:** `1, 20250816, 424242, 99991, 2147483647`. Committed rather than random so
  that a failure is replayable — the seed is in the job name and
  `pytest -p randomly --randomly-seed=<n>` reproduces it exactly.
- **The scheduled newer-minor check queries `python.org/api/v2/downloads/release/`, filtering
  `pre_release`.** Authoritative for "a *stable* CPython minor" in a way an EOL-date aggregator is
  not, and it needs no dependency beyond `urllib`. Verified live (Evidence D).
- **`newer-python-minor` is the only job gated by trigger** (`schedule` / `workflow_dispatch`). The
  other four run on `push` and `pull_request` as required checks. Nothing carries
  `continue-on-error`.

## Work Log

**2026-08-16 — implemented, self-verified, committed.**

*Change:* +`.github/workflows/ci.yml`, +`tests/test_packaging.py`. Boundary exact — no other repo file
was modified. (Scratch mutations were applied and reverted in place; `ci.yml`'s sha256 was re-checked
as `31da31d9…` after every one.)

### Five jobs, each gating the real artifact or the real interpreter set

| Job | Gates |
|---|---|
| `lint-type-test` | `flake8 .` · `mypy async_gateway` · `pytest` · `bandit -c pyproject.toml -r async_gateway -ll`, across the committed matrix `3.10–3.14` |
| `build-and-install` | clean-checkout assert → `python -m build` → clean-venv **wheel** install + submodule import → clean-venv **sdist** install (`--no-binary :all:`) + submodule import → `twine check dist/*` |
| `shuffled-order` | `pytest -p randomly --randomly-seed=<n>` over five committed seeds |
| `coverage-ratchet` | head's `fail_under` ≥ base branch's |
| `newer-python-minor` | monthly; red once CPython ships a stable minor above the matrix ceiling |

### The four carried-forward AGW-3 defects

1. **bandit is not auto-configured — honoured.** The step is
   `bandit -c pyproject.toml -r async_gateway -ll`. Without `-c`, bandit reads only `.bandit` files and
   `[tool.bandit]` would be silently ignored: an unconfigured scanner reporting green.
2. **The `bandit[toml]` fix has still never run on a real 3.10 leg — and cannot here.** This machine
   has CPython 3.11 / 3.12 / 3.14 only; **there is no 3.10 interpreter**. Everything below was executed
   on 3.14 (dev venv) or 3.12 (clean venvs). *What CI will verify that local cannot:* that on the
   `3.10` matrix leg `pip install -e '.[dev]'` pulls `tomli` via the `[toml]` extra's
   `python_version < "3.11"` marker, and that `bandit -c pyproject.toml …` therefore loads
   `[tool.bandit]` instead of failing to parse it. Until that leg runs green, the 3.10 claim rests on
   the declared marker, not on an execution.
3. **`python -m build --no-isolation` would fail — so CI does not use it.** `build` requires
   `setuptools>=77.0.3` while the dev extra pins `setuptools<76` (to keep flake8-import-order's
   `pkg_resources` import warning-free); the two cannot coexist in one interpreter. The
   `Build the wheel and the sdist` step is a plain isolated `python -m build`, and the reason is
   recorded in a comment above it so a later "speed it up with `--no-isolation`" edit is pre-answered.
   Confirmed harmless in practice: the local isolated build exited 0 from a venv carrying
   setuptools 75.9.1.
4. **`MANIFEST.in`'s comment is wrong and was NOT fixed here — `MANIFEST.in` is outside this
   story's boundary.** The comment claims its `prune` directives keep *the wheel* lean; `prune` affects
   the **sdist** only, and wheel content is decided by `[tool.setuptools.packages.find]`. The
   statement is misleading, not harmful (both exclusions do in fact hold, by different mechanisms).
   **Routed to the orchestrator** — flagged in the S4 report for assignment.

### Evidence — every regression proven mechanically, locally, against the check CI runs

GitHub Actions cannot be executed on this machine, so each guard was driven directly. Scratch
mutations were never committed.

**A — deleting `3.14` from the matrix turns a job red.**
`pytest tests/test_packaging.py` → **exit 1**, `1 failed, 3 passed`:

```
AssertionError: CI matrix [(3, 10), (3, 11), (3, 12), (3, 13)] and pyproject classifiers
[(3, 10), (3, 11), (3, 12), (3, 13), (3, 14)] disagree; ... Right contains one more item: (3, 14)
```

**A2 — skipping a minor (dropping `3.12`) turns a job red.** Same command, `-k contiguous` →
**exit 1**: `CI matrix [(3, 10), (3, 11), (3, 13), (3, 14)] is not a contiguous ascending run of
minors starting at 3.10`.

**B — lowering `fail_under` turns a job red.** The ratchet script was *extracted verbatim from
`ci.yml`* (24 lines, pulled out of the `coverage-ratchet` heredoc by a YAML load, so the thing run is
the thing CI runs) and executed twice:
- base `fail_under = 0`, head `0` → `coverage ratchet holds.` **exit 0**
- base `fail_under = 85`, head `0` → `coverage ratchet broken: head 0.0 < base 85.0.` **exit 1**

*Direction note, stated plainly:* the real base branch is `main`, which predates declarative
packaging and has **no `pyproject.toml`**, so its floor reads 0 and head cannot currently go *below*
it. The mechanism was therefore proven by raising the base side — arithmetically the same comparison.
The job will bite for real from the first base branch that carries a non-zero `fail_under`.

**C — re-adding (in effect, still having) `ujson` turns a job red.** This one needed no mutation:
the defect is live. `python -m build` → exit 0, both artifacts. Clean venv (3.12, `--no-cache-dir`),
`pip install dist/*.whl` → exit 0. Then, from `/tmp` so the source tree cannot shadow the install:

```
from async_gateway.async_gateway import request
  -> ModuleNotFoundError: No module named 'ujson'        IMPORT EXIT=1
```

and, proving the R1 edge case in the same breath, `import async_gateway` → **exit 0** (`__init__.py`
is 0 bytes, so the bare import passes while every submodule is broken — which is precisely why the
job imports the submodule). The **sdist** leg was run too, in its own clean 3.12 venv:
`pip install --no-cache-dir --no-binary :all: dist/*.tar.gz` → **exit 0** (68 wheels built from
source), then the same submodule import → **exit 1**, same `ModuleNotFoundError: No module named
'ujson'`. `twine check` → **PASSED ×2, exit 0**. This job is *expected red* until AGW-5 lands; that
is the story working, not a misconfiguration.

**D — the scheduled newer-minor job.** Run live against `python.org`:
`highest committed matrix entry = (3, 14)` / `highest stable CPython minor = (3, 14)` → **exit 0**.
Against a scratch copy with `3.14` removed → **exit 1**:
`CPython 3.14 is stable but the CI matrix stops at 3.13.`

**E — the suite and the linter.** `pytest` → **7 passed, exit 0** (AGW-1's 3 + this story's 4).
`pytest -p randomly --randomly-seed=20250816` → **7 passed**, so the shuffled-order job's premise
holds on at least one committed seed locally. `flake8 tests/` → **exit 0, no output**. The workflow
parses: `yaml.safe_load` lists all five jobs and the four triggers; both embedded Python heredocs
`ast.parse` cleanly.

### Finding raised by this story (needs orchestrator routing)

**Low — `--no-binary :all:` on the sdist leg builds the *entire dependency tree* from source.**
R1-AC and the DoD name the flag verbatim, so it is implemented verbatim, and it **does work**: the
local run exited 0 having built **68 wheels** from source, `orjson` and `cryptography` (both Rust)
included. But `:all:` is not scoped to `async-gateway` — it rebuilds every transitive dependency, so
the leg is slow and carries a large failure surface belonging to other projects (a broken upstream
sdist, or a runner image without a Rust toolchain, reddens this job for a reason unrelated to this
package). The *stated intent* — "a cached pip wheel must not satisfy the clean-venv install" — is
already fully met by `--no-binary async-gateway` plus the `--no-cache-dir` / `PIP_NO_CACHE_DIR`
in place. Narrowing the flag would be a **spec amendment**, not an implementation choice, so it is
escalated rather than taken. Severity is Low, not Medium: it works today and only costs time.

*Recorded honestly against an earlier draft of this log:* this was first written up as a Medium that
"could not be completed to a verdict locally, no Rust toolchain". That was wrong — the background run
had not finished when the note was written, and it subsequently exited 0. The verdict now cited is
the real captured one.

### Not done, deliberately

- **No additional flake8 "baseline suppression file".** Step 3 names one; `.flake8` (created by AGW-3)
  is the config the step reads, and it is **outside this story's boundary**, as is any new root-level
  config file. `flake8 .` currently reports **1284 pre-existing violations** — R27/AGW-25 owns driving
  that to zero. The step is therefore red-by-inheritance today, exactly like the `ujson` import job.
- **No `pyproject.toml` edit** to add PyYAML or `build`/`twine` to the dev extra. The build front-end
  is installed inside the `build-and-install` job instead, and the tests avoid YAML entirely.
