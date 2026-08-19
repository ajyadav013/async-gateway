# AGW-1: Suite can fail, runs async, is measured

- **Status:** IN REVIEW
- **Story:** S1 — spec Step 1, size M (`docs/specs/v1_release_stories.md` §4, Phase 0)
- **Spec:** `docs/specs/v1_release_spec.md` — R2, R27-AC1, R28-AC1,2,3 (Group A — Scaffolding and quality-gate infrastructure)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** none
- **Files (declared scope):** +`tests/conftest.py`, `tests/fixtures/{__init__,http_server}.py`, `tests/test_fixture_smoke.py` · ~`setup.cfg`, `requirements-dev.txt` · −`tests/test_pass.py`, `tests/coverage_output.py`

## Why

R2 requires test tooling that can actually run an async test; R27-AC1 and R28-AC1,2,3 require the
runners to be configured to see something rather than nothing. Today the suite cannot fail, cannot run
an unmarked `async def` test, and measures no coverage — so every later gate in this release would be
standing on an instrument that reads zero regardless of reality. This story also seeds the
`tests/fixtures/` layout that convention C-2 depends on.

Closes findings: C8, M26, M27, H19. Discharges FI-5.

## Definition of Done

- `grep -rn "pre_test" .` → 0 outside spec/audit
- fresh venv `flake8 --version` exits 0
- unmarked `async def` test runs
- `pytest -p randomly --randomly-seed=1` exits 0
- a deliberately failing test on a scratch branch makes pytest exit non-zero, **recorded in the work log** (not committed)
- `--cov=async_gateway --cov-branch` on, `fail_under` = then-measured floor
- **no aiohttp-mocking library in the dev set** (Ruling A)
- **FI-5:** `coverage_output.py` deleted in the same commit that enables `--cov-branch`

## Dependencies

- **blockedBy:** none
- **blocks:** AGW-2

## Decisions

- **Orchestrator Ruling F — `setuptools` ceiling tightened `<81` → `<76`.** The developer escalated
  rather than improvising, and the orchestrator reproduced it independently in clean venvs:
  setuptools 80.10.2 → `flake8 tests/` exits 0 **but emits** `pkg_resources` deprecation warnings;
  setuptools 75.9.1 → exit 0, **output_bytes = 0**. S25/R27-AC2 requires `flake8 .` to exit 0 *with no
  output*, not merely exit 0, so `<81` is unachievable-by-construction for that gate. `<76` ⊂ `<81`,
  so this tightens the spec's constraint rather than contradicting it. Fixed here because S3 folds
  `requirements-dev.txt` into `pyproject.toml` two stories from now, after which the fix is more
  expensive and harder to attribute.
- **Ruling A upheld:** no aiohttp-mocking library. `aioresponses` is broken against aiohttp 3.14.x, so
  the double is a real loopback `aiohttp.web` server (`tests/fixtures/http_server.py`).
- **Convention C-2 seeded:** `tests/conftest.py` holds imports + `__all__` only; the double lives in
  its own `tests/fixtures/` module. Six later stories add doubles without touching `conftest.py`.

## Work Log

**2026-08-16 — implemented, validated, reviewed, committed.**

*Changes:* `setup.cfg` `addopts` `-k pre_test --cov=. tests/` → `--cov=async_gateway --cov-branch`,
plus `testpaths`, `asyncio_mode = auto`, and a `[coverage:report] fail_under`.
`requirements-dev.txt` +`pytest-asyncio` +`pytest-randomly` +`setuptools<76`. Added
`tests/conftest.py`, `tests/fixtures/{__init__,http_server}.py`, `tests/test_fixture_smoke.py`.
Deleted `tests/test_pass.py`, `tests/coverage_output.py`.

*Files (`git diff --name-only`, deletions from the index):* `requirements-dev.txt`, `setup.cfg`,
`tests/coverage_output.py` (D), `tests/test_pass.py` (D), `tests/conftest.py`,
`tests/fixtures/__init__.py`, `tests/fixtures/http_server.py`, `tests/test_fixture_smoke.py`.
**Exactly the declared boundary — no out-of-scope path touched.**

### DoD evidence — orchestrator's own re-run, not the developer's self-report

- **The suite can now fail (C8 refuted).** The DoD requires this be recorded here. The orchestrator
  wrote a scratch file with two failing tests, one sync and one **unmarked `async def`**, ran it, and
  captured:
  ```
  FAILED tests/test_orch_validate_scratch.py::test_orch_unmarked_async_actually_runs_and_fails
  FAILED tests/test_orch_validate_scratch.py::test_orch_sync_fails
  2 failed, 3 passed
  REAL_EXIT_CODE=1
  ```
  Scratch file then deleted; `CLEAN_EXIT_CODE=0`. The unmarked async test being **collected and run**
  is the direct proof of async capability — under the old `-k pre_test` config it could not have been
  collected at all. Not committed.
- `pytest` → `3 passed`, exit 0.
- `grep -rn "pre_test"` → zero hits in any config or source file (remaining hits are the spec, the
  story breakdown and this ticket, which quote the criterion).
- `flake8 --version` → exit 0. `flake8 tests/` → exit 0, zero violations.
- `pytest -p randomly --randomly-seed={1,2,42,12345}` → exit 0 on all four.
- No aiohttp-mocking library in the dev set (Ruling A) — verified by grep.
- **FI-5** — `coverage_output.py` deletion and `--cov-branch` land in this one commit.
- `fail_under = 0` is the **measured** floor (`TOTAL 441 441 0%`), not a guess. It reads 0 because no
  `async_gateway` module is importable until the ujson migration at AGW-5; the ratchet raises it
  per step from here.

### Code review — APPROVED (1/5 iterations)

0 Critical · 0 High · 0 Medium · 5 Low · 3 Cosmetic → gate PASS. The reviewer probed the fixture live
on both aiohttp 3.9.5 and 3.14.3 and confirmed multi-value/case-insensitive header preservation,
per-hop redirect recording, recording of unregistered paths, and no cross-test state leakage.

*Low/Cosmetic carried forward (non-blocking, none lost):*
1. `http_server.py` docstring says it records what was "actually put on the wire", but `path`/`query`
   are percent-**decoded** (`/p8/a%2Fb` records as `/p8/a/b`); only `body` is byte-exact.
2. `body=await request.read()` is evaluated inside the `RecordedRequest(...)` call, so a client abort
   mid-upload records **nothing** — contradicting the "visible rather than silent" guarantee for the
   partial-send case.
3. `query`/`headers` field types are narrower than aiohttp's declared return types (latent only if
   test typing is ever enabled).
4. `ResponseSpec.headers` cannot express a repeated response header (e.g. two `Set-Cookie`).
5. `conftest.py`'s import list and `__all__` are append-only lists six later stories will each edit —
   the classic merge-conflict shape; consider `keep-sorted` markers.
6. (Cosmetic) the `requirements-dev.txt` comment says "five UserWarnings"; the count varies with
   worker count (12 observed). Substance verified correct.

**→ Items 1, 2 and 4 are routed to AGW-15, whose declared boundary already includes
`~tests/fixtures/http_server.py`.** Item 5 is offered to the integrator at the first C-2 merge.

*Reviewer note, accepted:* `respond()` is keyed on path only, with no per-method spec or response
sequencing. Correct as delivered (adding it now would be speculative configurability), but **AGW-15
must plan to extend the fixture** for its retry test ("one failure then one success" on one path),
its `asyncio.Event`-gated timeout handler, and a lying `Content-Length`.

*Commit:* see `git log --grep "\[AGW-1\]"`.
