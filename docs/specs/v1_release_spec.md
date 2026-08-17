# Specification: async-gateway v1.0.0 — Remediation and First Publish

**Status:** Revision 3 — revised against the Devil's Advocate plan critique
(`.claude/state/gate-evidence/devils-advocate-plan-critique.md`: **UPHELD** — C1 · M2 · L1). The
Critical (`aioresponses` is broken against aiohttp 3.14.x, detonating at Step 21 of 31) is closed by
dropping `aioresponses` entirely and moving the aiohttp upgrade into Phase 0; both Mediums and the Low
are closed. Revision 2 addressed EM review iteration 1
(`.claude/state/gate-evidence/em-review-iter1.md`: C0 · H2 · M12 · L10 · Cos5). Re-entering the plan
critique (stage 1e.5); `em-approved` is not final until it returns CONFIRMED.
**Repo:** `/Users/arjunsinghyadav/Practice/Personal/async-gateway/async-gateway`
**Branch / HEAD at spec time:** `v1.0rc-1` / `31542aa`
**Input backlog:** `.claude/state/repo-audit-report.md` (7-lane read-only audit, 2026-08-15)
**Target version:** `1.0.0` (down from the fork-inherited `2.7.3`)

---

## Overview

`async-gateway` is a ~1,133-LOC pure-Python async client library (22 `.py` files) that other projects
import to make HTTP / FTP / SFTP / SOAP calls behind one config-driven entry point,
`async_gateway.async_gateway.request()`. It is a fork, currently versioned `2.7.3`, and it has **never
been published**: `pypi.org/pypi/async-gateway/json` returns 404, `pypi.org/simple/async-gateway/`
returns 404, and `pip download async-gateway` reports no matching distribution. There are zero users,
zero consumers and zero production deployments.

That framing governs this entire document. **This is a pre-release checklist, not an incident report.**
Every defect below is real and every one bites on first publish — none of them has hurt anyone yet.
It is also what makes the plan cheap: with no consumers, breaking changes (a single response envelope,
a version reset, a renamed module) cost nothing today and cost a great deal the day after first upload.

The delivered state does not match the intent. From a clean install the documented entry point raises
`ModuleNotFoundError` (`ujson` imported by three runtime modules, declared nowhere). FTP raises
`UnboundLocalError` on every call. SFTP raises *after* completing transfers, including destructive
ones. `logic/soap.py` is a 0-byte file while the package title, the parameter table and `setup.py`
all advertise SOAP. `known_hosts=None` accepts any SSH host key. All three protocol classes wrap
their bodies in `except Exception` and return a fabricated `status_code: 999`, and the library has no
logger anywhere. Every quality control is configured to see nothing: `-k pre_test` deselects the whole
suite, `mypy ignore_errors = True` hides every error, `flake8` cannot start on a clean install, there
is no CI, and real line coverage of the library is 0%.

**Scope of this release.** Fix all of it and ship `1.0.0`: all four protocols working (SOAP implemented
from scratch), one honest response contract, a real exception hierarchy and logger, no blocking I/O on
async paths, transport security on by default, upgraded dependencies, PEP 621 packaging, every quality
gate on and green, 100% line **and** branch coverage, and a consumer-facing README. Actual upload to
PyPI stays a manual human step and is out of scope.

**"Done" means** the five-item PR gate in R27 passes on a clean checkout: fresh-venv install of the
built wheel imports a submodule, linter reports zero violations, mypy reports zero errors with no
`ignore_errors`, the full suite is green at 100% line + branch, and `sdist` + `wheel` build and pass
`twine check`.

This is a **backend / library-only** change. There is no frontend, no deployable service, and no UI
surface; the DevOps gate applies only to CI, and the Observability gate applies only to the library's
own logging discipline.

---

## ID reconciliation note

**This section is a deliverable in its own right.** The audit report's headline count is wrong, and
its Low/Cosmetic block has no ids at all. Anything downstream that greps the report for a finding id
will mis-count unless it reads this first.

### The headline vs. the actual inventory

The report states, at `repo-audit-report.md:50`:

> **Counts (deduplicated, post-adversarial-review):** 7 Critical · 27 High · 27 Medium · 14 Low/Cosmetic.

Three of those four numbers are wrong, and the fourth (Critical) is right for a non-obvious reason.

| Severity | Headline | Actual | Why the headline is wrong |
|---|---|---|---|
| Critical | 7 | **7** (C1, C2, C3, C4, C6, C7, C8) | Correct — but **there is no C5 section.** `C5` survives only as a prose back-reference inside the H28 heading ("H28 (was C5)"). The adversarial pass reclassified it Critical → High after proving by execution that CPython *refuses* to build a client socket from a `PROTOCOL_TLS_SERVER` context, so the mTLS path fails **closed**. The id was deliberately vacated, not renumbered, so nothing citing C5 dangles. C-ids therefore run C1–C8 **with C5 empty**. |
| High | 27 | **29** (H1–H29) | The header was never updated after two late adversarial-pass edits. **H28** is the reclassified former C5. **H29** is a seam defect the adversarial pass added (three mutually incompatible response envelopes from `request()`). Note also that **H13 is a live id that points elsewhere**: it was down-rated to Medium and now reads as M25, but the H13 heading is retained as a pointer, so it must still appear in any complete inventory. |
| Medium | 27 | **28** (M1–M28) | **M28** (a third caller-dict mutation, `sftp.py:41`) was likewise added by the adversarial pass after the header was written. |
| Low/Cosmetic | 14 | **17** (`L1`–`L17`, assigned by this spec) | The block at `repo-audit-report.md:652-678` is unnumbered running prose. See below. |

**Total distinct numbered findings = 7 + 29 + 28 + 17 = 81.**

### The unnumbered Low/Cosmetic block — ids assigned here

The report's Low/Cosmetic section is a single paragraph of middle-dot-separated clauses with no ids.
Counting the clauses yields **16**, not 14, plus a trailing type-annotation-coverage *measurement*.
This spec assigns stable ids `L1`–`L17`.

**Grouping rule applied (stated so it can be challenged):**

1. **One cause repeated across many files → one id.** "Module docstrings are absent or content-free
   across the tree" is one defect covering `utils/__init__.py`, `helpers/__init__.py`,
   `"""Constants."""`, `"""Ftp."""` and others → **L14**. Likewise import-order violations across five
   files → **L15**, and the three annotated-subscript-target sites in `async_gateway.py` → **L2**.
2. **Different causes at different line references → separate ids**, even in the same file.
   `circuit_breaker_helper.py:18` (truthiness fallback makes `maximum_failures=0` unreachable) and
   `circuit_breaker_helper.py:54` (`isinstance(x, int)` silently rejects floats while accepting `bool`)
   are two different defects requiring two different fixes → **L9** and **L10**. Same for
   `setup.py:17` (missing `encoding=`) and `setup.py:37` (false `Production/Stable` classifier) →
   **L16** and **L17**.
3. **Two symptoms inside the *same* one-function edit → one id.**
   `date_helper.py` bundles "uses `pytz` where the stdlib suffices" — the import at `:6` and the
   import-time `pytz.timezone(TIMEZONE)` binding at `:9` — with "the function named `get_ist_now`
   accepts an arbitrary timezone" at `:12`; both are resolved by rewriting that one function →
   **L11**. Same reasoning for `exceptions.py:12` (implicit-Optional *and* legacy `typing.Text`) →
   **L12** and `sftp.py:22` (`self.remote_files` is dead *and* mistyped) → **L6**.

Applying that rule to 16 clauses, with clause 9 split in two, yields **n = 17**.

| Id | Finding | `file:line` |
|---|---|---|
| L1 | `List[aiohttp.TraceConfig()]` subscripts a generic with an instance (harmless only because CPython does not evaluate that annotation in function scope) | `logic/http.py:26` |
| L2 | Annotated subscript targets — legal, but neither evaluated nor stored; pure noise | `async_gateway.py:97,105,108` |
| L3 | Latency measured with `time.time()` instead of `time.monotonic()`; an NTP step yields negative latency on the exact metric users dashboard | `logic/http.py:64` |
| L4 | `start_time: int` annotation on a float | `helpers/internal/base.py:32` |
| L5 | Class docstring says "ftp request class" in the SFTP module | `logic/sftp.py:13` |
| L6 | `self.remote_files` is dead code and mistyped — and looks like the missing initialisation H2 needs, so it will mislead whoever fixes H2 | `logic/sftp.py:22` |
| L7 | Docstring documents `:param session - Sqlalchemy session object` in a library with no database | `helpers/internal/request_helper.py:88` |
| L8 | Structurally dead `if ...: pass` branch | `helpers/internal/filters_helper.py:62` |
| L9 | Truthiness fallback makes `maximum_failures=0` unreachable | `helpers/internal/circuit_breaker_helper.py:18` |
| L10 | `isinstance(x, int)` silently rejects floats while accepting `bool` | `helpers/internal/circuit_breaker_helper.py:54` (and the identical sibling at `:49`) |
| L11 | Uses `pytz` where the stdlib suffices (import at `:6`, import-time binding at `:9`); and `get_ist_now` is named for IST while accepting an arbitrary timezone (`:12`) | `helpers/common/date_helper.py:6,9,12` |
| L12 | Implicit-Optional and legacy `typing.Text` | `utils/exceptions.py:12` |
| L13 | `STATUS_CODE_403 = 403` — a constant named after its own value | `utils/constants.py:6` |
| L14 | Module docstrings absent or content-free across the tree (`utils/__init__.py` and `helpers/__init__.py` are 0 bytes; `"""Constants."""`, `"""Ftp."""`) | tree-wide |
| L15 | Import grouping violates stdlib / third-party / first-party ordering | `base.py`, `filters_helper.py`, `circuit_breaker_helper.py`, `ftp.py`, `sftp.py` |
| L16 | `open('README.md')` without `encoding=` (latent only — the README is currently pure ASCII) | `setup.py:17` |
| L17 | Claims `Development Status :: 5 - Production/Stable` for a package that does not import | `setup.py:37` |

**The type-annotation-coverage measurement is deliberately *not* an `L` id.** The report's trailing
paragraph (47 functions, 45 public, 28 lacking a return annotation, 22 with at least one unannotated
parameter, worst offenders being all 15 tracer callbacks, all three `handle_request()` overrides and
5 of 7 functions in `request_helper.py`) is a *measurement*, not a defect with a location. It is folded
into **R30** (source documentation and typing) and **R27** (mypy at zero errors with `ignore_errors`
removed), whose acceptance criteria are count-independent and mechanically checkable.

### A fourth discrepancy this spec found, which the brief did not anticipate

**The Medium section has an unnumbered prose block too.** `repo-audit-report.md:638-648` closes the
Medium section with an italicised "*Also Medium, grouped:*" paragraph containing **eight further
Medium-severity findings with no ids** — structurally identical to the Low block, but not flagged
anywhere in the report's headers or in this task's ground-truth inventory.

Dropping them would lose eight real findings, several of which are *directly named by the locked
human decisions* (`pyfailsafe` dormancy, `orjson`/`requests` declared-but-unimported, the README's
19 live corporate endpoints, the missing SFTP README section). Renumbering them as `M29`–`M36` would
contradict the verified ground truth that Medium is exactly `M1`–`M28`, and would break any
mechanical grep against that inventory.

**Resolution:** they are given a clearly-distinguished secondary series, **`MG1`–`MG8`** ("Medium,
Grouped"), are carried in the traceability table alongside everything else, and are flagged in
**Open Questions (OQ1)** for human ratification. The numbered inventory stays exactly
7 + 29 + 28 + 17 = **81**; the traceability tables carry **89 findings** because they also carry
`MG1`–`MG8`, in **90 physical rows** — the ninetieth being the deliberate placeholder for the vacated
`C5`.

| Id | Finding | Location |
|---|---|---|
| MG1 | `pyfailsafe==0.6.0` is dormant (sole release 2021-02-06, 5.5 years stale, no `requires_python`) and sits on the critical resilience path | `requirements.txt:6` |
| MG2 | `orjson` and `requests` are declared and imported by zero files | `requirements.txt:2,3` |
| MG3 | `TIMEZONE = 'Asia/Kolkata'` hardcoded into a published package and bound at import time with no override | `utils/constants.py:4`, `helpers/common/date_helper.py:9` |
| MG4 | `CHUNK_SIZE_CONSTANT = 1024` is ~64× smaller than conventional, costing ~100,000 await cycles on a 100 MB upload | `utils/constants.py:12` |
| MG5 | `fetch_file` is dead code; removing it also retires the entire `helpers/common/file_helper.py` duplicate | `helpers/internal/request_helper.py:13`, `helpers/common/file_helper.py` |
| MG6 | The README documents **no SFTP section at all** while SFTP is fully implemented and registered | `README.md` (zero `sftp` matches in 1,126 lines) |
| MG7 | The root `CLAUDE.md` "Commands" block documents `uvicorn app.main:app`, `ruff`, and an `app/` package for a library with none of those — actively misleading future contributors and agents | `CLAUDE.md` §Commands |
| MG8 | The README ships a live corporate endpoint (`api.fyndx1.de`) **19 times**, republished inside every sdist because `setup.py:17-18` reads the README and `setup.py:28` embeds it as `long_description` | `README.md` ×19; `setup.py:17-18,28` |

### A fifth discrepancy: the mypy error count contradicts itself

The report's executive summary (`:39`) says `ignore_errors = True` "hides 44 real errors"; the H18
finding body (`:426-430`) reports the measured run as **`Found 40 errors in 9 files`**. This task's
brief carries the 44. Both cannot be right, and neither is verifiable from this spec's read-only
position. **The discrepancy is deliberately not resolved here and is not allowed to matter:** R27's
acceptance criterion is `mypy async_gateway` exiting 0 with zero errors and no `ignore_errors` in any
config file — a count-independent condition. Flagged as **OQ2**.

---

## Requirements

Requirements are grouped, and the groups are ordered so the **scaffolding-first** sequencing the
audit's adversarial premortem insisted on is expressible to the story planner: infrastructure (R1–R5)
→ contract and error model (R6–R10) → protocol correctness (R11–R21) → resilience and security
(R22–R26) → gates turned on (R27–R28) → documentation and value-adds (R29–R34).

Every requirement's acceptance criteria are written to be individually checkable by a command, a test,
or a diff inspection. "Improved" and "better" do not appear as criteria anywhere; where a criterion
names a test, the test is the criterion.

Unless stated otherwise the persona is **the consumer**: a developer importing `async-gateway` into
another project to make API calls. Where the real persona is the maintainer or CI, the user story says
so explicitly.

---

### Group A — Scaffolding and quality-gate infrastructure

> **Sequencing constraint:** every requirement in this group lands *before* the bulk of the protocol
> fixes. Turning every gate on at once against a codebase with zero tests surfaces dozens of mypy
> errors and 1,290 flake8 violations simultaneously with no CI to bisect against, and the predictable
> response is to restore a suppression "temporarily" and lose the gate permanently.

#### R1: Continuous integration that builds, installs and imports the real artifact

**Description**: A GitHub Actions workflow that, on every push and pull request, builds the
distribution, installs it into a clean virtual environment, imports a **submodule** (not the
0-byte top-level package), and runs the linter, the type checker and the test suite.

**User Story**: As the **maintainer**, I want CI to install the built artifact into a clean venv and
import a submodule on every change, so that a package that does not import can never reach an index
again.

**Acceptance Criteria**:
- [ ] `.github/workflows/ci.yml` exists and triggers on `push` and `pull_request`.
- [ ] The workflow has a job that runs `python -m build`, then `pip install dist/*.whl` in a fresh
      venv containing no project dependencies, then `python -c "from async_gateway.async_gateway import request"`, and that job **fails** if the import raises.
- [ ] The workflow additionally installs from the **sdist** (`pip install --no-binary :all: dist/*.tar.gz`) in a separate fresh venv and imports the same submodule — this is the path H26 breaks.
- [ ] The workflow runs, as separate named steps: `flake8 .`, `mypy async_gateway`, `pytest`,
      `bandit -r async_gateway -ll` (see below), and `twine check dist/*`.
- [ ] **A Python version matrix.** The lint/type/test job runs on **every released minor interpreter
      in the declared `requires-python` range**: `3.10`, `3.11`, `3.12`, `3.13`, **`3.14`** for
      `>=3.10`. *(CPython 3.14 is released — the plan critique's host reports `python3 -V` →
      `3.14.7`, and it installed the full target dependency set and imported every runtime package
      on it. An unbounded `>=3.10` admits 3.14, so omitting it understated the ceiling by one minor
      version at exactly the point the next line says the ceiling is load-bearing.)*
- [ ] **The matrix list is committed, not derived — and the anti-drift mechanism is stated rather
      than claimed.** `requires-python` has no upper bound, so **no test can generate a finite matrix
      from it**; an earlier draft asserted the list "is generated from `requires-python` … so the
      matrix cannot drift from the metadata", which is not implementable and had in fact already
      drifted. Two mechanical checks replace that claim:
      1. a test asserts every entry in the committed matrix is **≥ the `requires-python` floor** and
         that the entries are **contiguous** (no skipped minor);
      2. a **scheduled CI job** (monthly) fails when a stable CPython minor **newer than the matrix's
         highest entry** exists, so a new release is a visible maintenance event rather than silent
         staleness.
      The alternative — bounding `requires-python` (e.g. `>=3.10,<3.15`) so the range *is* finite — is
      rejected here because a ceiling on a library blocks consumers on the day a new interpreter ships;
      it is raised for ratification in **OQ3**.
      The matrix is the mechanical proof behind the NFR table's portability claim and behind OQ3, and
      it is the interpreter *ceiling* — not the floor — that R7's dormancy evidence actually needs.
- [ ] **A shuffled-order job.** A named step runs `pytest -p randomly` over **five fixed seeds**
      (`-p randomly --randomly-seed=<n>`, n ∈ a committed list) and every run exits 0. This is the
      runnable form of R28's order-independence criterion and it requires `pytest-randomly` (R2).
- [ ] **`bandit` actually fires.** `bandit -r async_gateway -ll` runs as a named required step and
      exits 0. `bandit` is currently a declared dev dependency that no command ever invokes — a
      control that cannot fire is the C8/H18/H19 pattern this release exists to end. If the maintainer
      would rather not run it, the dependency is **removed** rather than left declared and dead.
- [ ] **The coverage ratchet is enforced by CI.** A named step reads the `fail_under` value on the
      base branch and on the head commit and **fails the job if the head value is lower**. See R28.
- [ ] Every step is a required check; none is `continue-on-error` at the end of the release.
- [ ] A deliberate regression test of CI itself is recorded once in the ticket work log: with `ujson`
      re-introduced as an undeclared import on a scratch branch, the clean-venv-import job fails. (Evidence, not a permanent test.)

**Edge Cases**:
- Importing only `import async_gateway` succeeds even when the package is broken, because
  `__init__.py` is 0 bytes — the check **must** import `async_gateway.async_gateway`.
- The build must run from a *clean checkout*, not the developer's working tree, so a file present
  locally but missing from the sdist (H26) still fails.
- A cached pip wheel from a previous run must not satisfy the clean-venv install; the job disables the
  wheel cache or uses `--no-cache-dir`.

---

#### R2: Test tooling that can actually run an async test

**Description**: Add the missing dev dependencies and configuration that make it possible to write a
single async unit test, and remove the two artefacts that break the moment coverage configuration
changes.

**User Story**: As the **maintainer**, I want a contributor's first `async def` test to fail on their
assertion rather than on a tooling error, so that the test suite is writable at all.

**Acceptance Criteria**:
- [ ] `pytest-asyncio` is declared in the dev dependency set and configured with `asyncio_mode = "auto"`; an `async def test_...` in `tests/` runs without a per-test marker.
- [ ] **The HTTP/SOAP test fixture is a local `aiohttp.web` server driven through
      `aiohttp.test_utils.TestServer` — and no aiohttp-mocking library is added to the dev dependency
      set.** `aioresponses` is **not** used: A5 records the executed evidence that
      `aioresponses 0.7.9` — the terminal release, with no newer or pre-release version — raises
      `TypeError: ClientResponse.__init__() missing 1 required keyword-only argument: 'stream_writer'`
      on **every** mocked request against the entire aiohttp 3.14.x line, while resolving cleanly
      against `aiohttp>=3.14.3,<4`. It is not pinned to an older aiohttp "for some tests" either: a
      second aiohttp in the dev set is the same latent trap one layer down.
      The fixture is a `conftest.py` helper that starts a `TestServer` whose handler **records the
      request it received** (method, path, headers, raw body bytes) and returns a caller-specified
      status, headers and body. Every assertion the aioresponses-based criteria used to make is made
      against that recording — per-hop redirect behaviour, request-body byte-identity, header
      emission — and the plan critique verified the mechanism on aiohttp 3.14.3
      (`RESULT: (200, 'pong')`, `CAPTURED: {'body': '<Envelope/>', 'ctype': 'text/xml'}`).
- [ ] **Day one, the fixture is exercised against the *target* aiohttp — not against the one currently
      pinned.** In a scratch venv carrying the dev set plus `aiohttp>=3.14.3,<4`, one request is driven
      end-to-end through the fixture and the recorded request is asserted. This single command is what
      would have caught the `aioresponses` failure on day one instead of at Step 21 of 31, and it is
      the reason it is a Step 1 criterion rather than a Step 21 discovery. It also makes the general
      rule under **Assumptions** ("a *tool X supports version Y* assumption is closed only by executing
      a call through X against Y") executable at the earliest possible moment.
- [ ] **`pytest-randomly` is declared in the dev dependency set** and `pytest -p randomly --randomly-seed=1`
      exits 0 on the (then tiny) suite. It is required by R28's order-independence criterion and by
      R1's shuffled-order CI step; today `requirements-dev.txt:1-12` contains no such plugin, so that
      criterion would otherwise be unexecutable.
- [ ] `flake8` starts from a clean install: either `setuptools<81` is pinned in the dev set, or
      `flake8-import-order` is dropped. Verified by `pip install -r <dev deps> && flake8 --version` in a fresh venv exiting 0.
- [ ] `tests/test_pass.py` is deleted.
- [ ] `tests/coverage_output.py` is deleted (it is not a test — pytest never collects it — it is dead
      because no `--cov-report=xml` is configured, and its line 13 `int(attrib.get('branch-rate'))` raises `ValueError` the instant `--cov-branch` is enabled).
- [ ] `-k pre_test` no longer appears in any configuration file (`grep -rn "pre_test" .` returns nothing outside this spec and the audit report).

**Edge Cases**:
- Enabling `--cov-branch` (R28) before deleting `coverage_output.py` breaks the script; the deletion
  is in *this* requirement precisely so it lands first.
- With `asyncio_mode = "auto"`, a plain sync test must still run; the config change must not
  re-gag the suite the way `-k pre_test` did.

---

#### R3: Runtime dependencies that are declared, imported, and correct

**Description**: Complete the abandoned `ujson` → `orjson` migration at all **four** `ujson`
references — three direct call sites (`helpers/internal/response_helper.py:14`,
`helpers/internal/filters_helper.py:45`, `:78`) plus the `json_serialize` default wired at
`logic/http.py:30` — handle `orjson.dumps()`'s `bytes` return contract at each of them, and drop the
unused `requests` dependency. *(The audit's prose said "three"; a `grep -rn ujson async_gateway/`
returns four.)*

**User Story**: As a developer importing async-gateway, I want `from async_gateway.async_gateway import request` to work immediately after `pip install async-gateway`, so that the documented entry point is usable.

**Acceptance Criteria**:
- [ ] `grep -rn "ujson" async_gateway/` returns zero matches.
- [ ] `orjson` is declared in the runtime dependency set with a range (not `~=`, not `==`), at the version fixed by R6.
- [ ] `requests` is removed from the runtime dependency set, and `grep -rn "^import requests\|^from requests" async_gateway/` returns zero matches.
- [ ] **All four former `ujson` sites are migrated** — the three direct call sites listed below, plus
      `logic/http.py:30`'s `json_serialize` wiring, which the next criterion owns. *(Four sites across
      **three** modules: `filters_helper.py` carries two of them, at `:45` and `:78`. The module count
      is three — matching C1's three import citations — and the site count is four; both numbers are
      used deliberately and are not interchangeable.)* Each of the three direct call sites handles the
      `bytes` return explicitly:
      - `helpers/internal/response_helper.py:14` — `ujson.loads(response)` → `orjson.loads(response)`. `orjson.loads` accepts `str` **and** `bytes`; no change of contract. A test asserts both input types parse.
      - `helpers/internal/filters_helper.py:45` — `ujson.dumps(form_value)` inside `FormData.add_field`. `orjson.dumps` returns `bytes`; `FormData.add_field` accepts `bytes` but will then send it without a text content type. The migrated call **decodes to `str`** (`orjson.dumps(v).decode()`) so the emitted form field is byte-identical to today's. A test asserts the field value is a `str` and round-trips.
      - `helpers/internal/filters_helper.py:78` — `data = ujson.dumps(data)` feeding `filters = {'data': data}`. The migrated call **decodes to `str`**; a test asserts `isinstance(filters['data'], str)`.
- [ ] The **`json_serialize` trap is handled**: `logic/http.py:30` reads `self.serialization` from
      `protocol_info` (defaulting to the JSON dumper) and passes it to `aiohttp.ClientSession(json_serialize=...)`, which requires a **`str`-returning** callable. The default is therefore a wrapper (`lambda obj: orjson.dumps(obj).decode()`), never bare `orjson.dumps`. A test posts a JSON body through the default serializer against the recording `aiohttp.web` handler (R2) and asserts the body the handler received decodes to the expected JSON.
- [ ] A caller-supplied `serialization` callable that returns `bytes` is rejected at the boundary with a `ConfigurationError` (R10's error model) rather than failing deep inside aiohttp. A test asserts this.
- [ ] The R1 clean-venv submodule import passes.

**Edge Cases**:
- `orjson.dumps` raises `TypeError` on types the stdlib `json` module serialises via `default=` (e.g. `datetime` is supported natively by orjson, but arbitrary objects are not). Serialisation failures surface as a `ConfigurationError`, not a fabricated status code.
- A caller who explicitly passes `serialization=json.dumps` must keep working; the wrapper applies only to the default.
- `orjson.loads` on empty bytes raises `orjson.JSONDecodeError` (a `ValueError` subclass) — M9/M8's handling must catch the `ValueError` base so the migration does not change which exception type escapes.

---

#### R4: PEP 621 packaging that builds a correct sdist and wheel

**Description**: Replace executable `setup.py` metadata with a declarative `pyproject.toml`, fix the
sdist file set, declare `python_requires`, correct the classifiers, and add the `py.typed` marker
(only after mypy is clean).

**User Story**: As a developer installing async-gateway, I want correct, resolver-visible metadata and
a working source distribution, so that installation succeeds on wheel and non-wheel paths alike and my
type checker can see the library's annotations.

**Acceptance Criteria**:
- [ ] `pyproject.toml` exists with a `[build-system]` table (PEP 517) and a `[project]` table (PEP 621) carrying `name`, `version`, `description`, `readme`, `license`, `authors`, `urls`, `requires-python`, `dependencies`, and `[project.optional-dependencies].dev`.
- [ ] `setup.py` and `setup.cfg` are deleted; remaining tool configuration (`flake8`, `mypy`, `pytest`, `coverage`) lives in `pyproject.toml` where the tool supports it and in a dedicated file (`.flake8`) where it does not.
- [ ] `requires-python = ">=3.10"` is declared (see **OQ3**), and installing on an unsupported interpreter produces pip's clean `Requires-Python` message rather than a transitive-resolution cascade.
- [ ] The sdist contains everything the build needs: `python -m build --sdist`, then in a scratch directory `tar -tf dist/*.tar.gz` includes `README.md`, `LICENSE`, and `pyproject.toml`, and `pip install --no-binary :all: dist/*.tar.gz` succeeds in a clean venv. (H26's `FileNotFoundError` on `requirements.txt` disappears because the build no longer reads a file at build time.)
- [ ] `async_gateway/py.typed` exists and is included in the wheel — **added only after R27's mypy criterion is green**. A downstream project running mypy against `async_gateway` sees the annotations.
- [ ] Classifiers are truthful: `Development Status :: 4 - Beta` or `5 - Production/Stable` chosen deliberately and justified in the CHANGELOG entry; the Python version classifiers match `requires-python`.
- [ ] `twine check dist/*` passes for both artifacts.
- [ ] No file is read with `open()` without an explicit `encoding=` anywhere in the build path.

**Edge Cases**:
- Adding `py.typed` before mypy is clean exports the library's own type errors to every downstream
  consumer; the ordering constraint above is load-bearing and is repeated in the implementation steps.
- `find_packages(exclude=(...))` semantics must be preserved: `tests`, `docs` and `local_development` stay out of the wheel. Verified by `unzip -l dist/*.whl`.
- A build run from a dirty working tree can accidentally include untracked files; the R1 CI job builds from a clean checkout.

---

#### R5: Version reset to 1.0.0 with a single source of truth and clean provenance

**Description**: Collapse four contradictory version claims into one, tag the release, and remove the
provenance errors that point at another organisation's artifacts.

**User Story**: As the **maintainer**, I want exactly one place where the version lives and a git tag
that matches it, so that a future bug report can be bisected to a released version.

**Acceptance Criteria**:
- [ ] The version is `1.0.0` and is declared in exactly one place; `async_gateway.__version__` is
      importable and equal to the built distribution's version. A test asserts
      `importlib.metadata.version("async-gateway") == async_gateway.__version__`.
- [ ] `docs/source/conf.py`'s `release = '2.1'` no longer exists (the file is removed by R31) or is derived from `async_gateway.__version__`.
- [ ] `grep -rn "2\.7\.3" .` returns no matches outside the audit report, this spec, and the CHANGELOG's history section.
- [ ] An annotated git tag `v1.0.0` exists on the release commit, and it is the repository's first tag.
- [ ] `download_url` no longer appears in any packaging metadata. (It currently points at
      `https://github.com/gofynd/aio-requests/archive/refs/tags/v2.7.3.tar.gz` — a *different project's* release tarball — while `url` points at `https://github.com/ajyadav013/async-gateway`.)
- [ ] `project.urls` contains only URLs that resolve to this repository.
- [ ] The version-bump procedure documented in the README (R29) names exactly one file to edit.

**Edge Cases**:
- The branch name `v1.0rc-1` is not a valid PEP 440 version string and must not be used as one.
- Because PyPI 404s, there is no published `2.7.3` to be downgraded from; the reset is safe *only*
  while that remains true. If the package is uploaded before this lands, the reset becomes impossible
  and the version must go forward instead — a one-way door, called out in **OQ4**.

---

### Group B — Dependency upgrades and the resilience-library decision

#### R6: Upgrade to the pre-approved dependency versions and loosen library pins

**Description**: Move every runtime dependency to the human-approved pinned version and convert the
library's exact `==` pins to sane ranges — **in Phase 0, at Step 1.5, before a single test is
written.**

> **Placement: Phase 0, Step 1.5 (orchestrator Ruling B, closing the plan critique's Critical).**
> An earlier revision deferred this to Step 21 of 31 so that the three surfaces the aiohttp
> 3.9 → 3.14 migration touches would already be correct and tested. That trade is **reversed**, for
> two executed reasons:
>
> 1. **The migration risk it was hedging against is small.** `inspect.signature` on aiohttp 3.14.3
>    shows `verify_ssl` **and** `ssl` still present on `ClientSession._request` and on
>    `TCPConnector.__init__`, and the `TraceConfig` callbacks intact. The three "changed surfaces" are
>    in any case being **rewritten** by R23 (Step 15), R25 (Step 18) and R26 (Step 19) — with their own
>    tests — so there is no legacy code being migrated blind. Upgrading first means those rewrites are
>    written **once**, against the transport they will ship on.
> 2. **The risk the deferral created is much larger.** Everything built between Step 1 and Step 21 is
>    built against a transport the release then replaces, and Step 21's gate is a full re-run of that
>    work. Any tooling incompatibility with the target aiohttp therefore surfaces at maximum schedule
>    pressure, against a `fail_under` ratchet that forbids lowering the number. That is exactly how the
>    `aioresponses` defect (A5) became project-stopping rather than a day-one annoyance.
>
> Moving it here also closes **C7** — a Critical carrying 35 advisories — at Step 1.5 instead of
> Step 21, so the library is no longer rewritten on top of it. **Step 21 is retained and vacated**
> (see the implementation steps) so nothing citing it dangles.

**User Story**: As a developer whose project already depends on `aioboto3` or `pytz`, I want
async-gateway to declare version *ranges* rather than exact pins, so that installing it does not
force a resolver conflict or freeze me on 2024 timezone rules.

**Pre-approved target versions (locked human decision; *resolver*-verified 2026-08-15 and re-verified
since — see the note under Dependencies §Runtime for what a resolver check does and does not prove):**
`aiohttp 3.14.3` · `orjson 3.12.0` · `aioboto3 15.5.0` · `aiofiles 25.1.0` · ~~`pytz 2026.3.post1`~~
· `aioftp 0.28.0` · `asyncssh 2.24.0`.

> **Amendment to the pre-approved list (orchestrator Ruling 3, strictly reducing).** `pytz` is
> **removed**, not upgraded. R9 now emits the envelope timestamp as UTC ISO-8601 and — after the
> scope cut recorded under "Deliberately cut sub-features" — performs **no IANA timezone lookup at
> all**, so `datetime.timezone.utc` from the stdlib `datetime` module is sufficient and neither
> `zoneinfo` nor a `tzdata` dependency is required on any platform. The pre-approved
> `pytz 2026.3.post1` pin survives **only** as a fallback if removal proves infeasible during R9; if
> that happens, the reason is written into the R9 story before the pin is restored. Deviating from a
> pre-approved list needs a human to see it, so this is flagged in **OQ11**.
>
> **`pytz`'s removal is the one part of R6 that cannot move to Phase 0.** `helpers/common/date_helper.py`
> imports `pytz` at module scope and binds `pytz.timezone(TIMEZONE)` at import time, so dropping the
> declaration at Step 1.5 would turn Step 3's clean-venv submodule import red. Step 1.5 therefore
> carries `pytz` at the pre-approved `>=2026.3.post1,<2027` range, and **R9 (Step 5) removes the import
> and the declaration in the same commit**. Same for `requests`: R3 (Step 4) owns its removal.

**Acceptance Criteria**:
- [ ] Every runtime dependency is expressed as a range with a floor at the approved version and a
      major-version ceiling (e.g. `aiohttp>=3.14.3,<4`). `grep -c "==" ` over the dependency table
      returns 0.
- [ ] **`pytz` is removed** (Ruling 3) — **by R9 at Step 5**, not at Step 1.5, because `date_helper`
      still imports it in Phase 0. From Step 5 onward, `grep -rn "pytz" .` returns nothing outside this
      spec, the audit report and the CHANGELOG's history section, and it appears in no dependency set.
      If R9 finds removal infeasible, the fallback is the pre-approved `>=2026.3.post1,<2027` range
      **with the reason recorded in the R9 story and the ADR index** — never a silent restore.
- [ ] **No aiohttp API migration is performed at Step 1.5, and that is the point.** The three surfaces
      the 3.9 → 3.14 range touches — the connector `ssl=` / `verify_ssl=` keyword (R23, Step 15),
      the `TraceConfig` callback signatures (`utils/request_tracer.py`, R26, Step 19) and the timeout
      API (`ClientTimeout`, R14, Step 13) — are **rewritten from scratch by those requirements against
      the already-upgraded set**, each with its own tests, so there is no separate migration step and
      nothing is written twice. Executed evidence that the existing code survives the interval in
      between: on aiohttp 3.14.3, `verify_ssl` and `ssl` are both still present on
      `ClientSession._request` and `TCPConnector.__init__`, and the `TraceConfig` callbacks are intact.
- [ ] **Step 1.5's own gate is that the upgraded set resolves and imports** — not a re-run of tests
      that do not exist yet, and **not** an artifact check, because `pyproject.toml` does not exist
      until Step 2. Specifically, in a fresh venv: installing the upgraded runtime set resolves with no
      backtracking warning, and `python -c "from async_gateway.async_gateway import request"` succeeds
      against the source tree. That import is what catches a module-level import broken by 15.x-era
      `aioboto3` or 2.24 `asyncssh` — see **FI-11** — at the cheapest possible moment. Additionally,
      Step 1's fixture round-trip is re-run against the now-declared aiohttp rather than against a
      scratch install. *(The artifact-level form of this check — build, install the wheel and the
      sdist into clean venvs, import a submodule — arrives with R4 at Step 2 and R1 at Step 3, and it
      runs against this same set from then on.)* This replaces the earlier "re-run R23's and R26's full
      test sets" gate, which only made sense while the upgrade landed after them; see **FI-10**.
- [ ] A dependency advisory scan over the *resolved* upgraded set runs **at Step 1.5**, so the advisory
      position is clean from Phase 0 onward, and **no story after Step 1.5 may merge on the un-upgraded
      set.**
- [ ] A dependency advisory scan over the *resolved* set reports zero Critical/High advisories.
      **The criterion is "zero Critical/High in the resolved set", not a delta against the audit's
      "38 vulnerabilities in 3 packages"** — that baseline included `requests` and `orjson`, both of
      which this release changes, so a count comparison would mis-report.
- [ ] The dependency table is documented in the README (R29) with the reason each dependency exists.

**Edge Cases**:
- `~=3.9.5` is a dead end **independent of any advisory data**: 3.9.5 is the last release ever
  published in the 3.9 line, so the range admits exactly one version, permanently. This criterion holds
  even if every CVE id in the audit is wrong.
- Loosening a ceiling to `<5` on a library whose 4.x does not exist yet is speculative; ceilings pin to
  the next major only.
- `aioboto3` 15.x removed the module-level `aioboto3.client(...)` factory that `utils/http_file_config.py:30` and `helpers/common/file_helper.py:23` still call (H15); the upgrade *forces* that fix, so R25 must land in the same change or the S3 path fails to import.

---

#### R7: Decide the circuit-breaker library on evidence, and write down the trade-offs

**Description**: `pyfailsafe 0.6.0` is already the latest release — the project is dormant (sole
release 2021-02-06, 5.5 years stale, no `requires_python`) — and it sits on the critical resilience
path. Test it under the upgraded aiohttp; if it proves unfit, either adopt a maintained replacement or
vendor a minimal circuit breaker. **Neither a silent keep nor a silent swap is acceptable.**

**User Story**: As the **maintainer**, I want the retry/circuit-breaker dependency decision made against
recorded evidence with the trade-offs written down, so that a future reader knows *why* the library
under `helpers/internal/circuit_breaker_helper.py` is the one it is.

**Decision procedure (this is the requirement — the outcome is not pre-judged):**

1. **Fitness test.** With the R6 dependency set installed, run the R28 suite's resilience tests against
   `pyfailsafe`. **Seven named behaviours:** retry on a transient failure, abort on an abortable
   exception, circuit opens after `maximum_failures`, circuit half-opens after
   `reset_timeout_seconds`, backoff with jitter spaces attempts, `Failsafe.run` correctly propagates a
   coroutine's exception with `__cause__` intact, and — **behaviour 7, added by orchestrator Ruling 2 —
   *clock and sleep injectability***: the timing behaviours above can be driven from the seam R24/R28
   require (`CircuitBreakerConfig.clock` / `.sleep`, Part B) without monkeypatching a module-global
   `time.monotonic` or `asyncio.sleep`.

   > **Behaviour 7 is already known-failed, by execution, at planning time — it is not an open
   > question for Step 22.** `pyfailsafe 0.6.0` (import name `failsafe`) hardcodes both seams:
   >
   > ```
   > failsafe/circuit_breaker.py:139:  self.opened_at = time.monotonic()
   > failsafe/circuit_breaker.py:142:  if time.monotonic() > self.opened_at + self.circuit_breaker.reset_timeout_seconds:
   > failsafe/failsafe.py:103:         await asyncio.sleep(wait_for)
   > ```
   >
   > No clock parameter, no sleep parameter, no seam. (Behaviours 1–6 remain genuinely open and are
   > what Step 22 measures. `failsafe` itself **imports cleanly** on 3.12.14 and on 3.14.7 alongside
   > the full target dependency set, so A8's import half holds.) Recording this here rather than
   > leaving it to be discovered at Step 22 is what takes an unestimated three-way decision out from
   > four steps upstream of Step 26's terminal coverage gate.
2. **Evidence that decides it.** `pyfailsafe` is **fit** if and only if behaviours 1–6 pass **on every
   interpreter in the declared `requires-python` range, as run by the R1 matrix** — not merely on the
   floor — it imports without a `DeprecationWarning` scheduled to become an error, and it introduces no
   transitive dependency outside the approved set. *Why the ceiling, not the floor:* MG1's defect is
   **dormancy**, and a 5.5-year-stale distribution with no `requires_python` fails at the newest
   interpreter, not the oldest. A criterion measured only at the floor resolves to "keep" almost
   regardless, which is not a decision procedure.
   **Behaviour 7 is scored separately and is not a pass/fail input to fitness** — and, being already
   known-failed (above), its three-way proposal is **resolved in this spec** rather than deferred:

   > **Decision (orchestrator Ruling D): KEEP `pyfailsafe`, WITH A NAMED COST — and R24 owns the
   > seam.** `helpers/internal/circuit_breaker_helper.py` becomes a thin **facade** the rest of the
   > library talks to, and **the facade holds `clock` and `sleep`**, rather than the plan expecting
   > `pyfailsafe` to grow a seam it does not have. That keeps the "keep" branch fully testable, so
   > R24's half-open criterion and R28's no-sleep rule have something to drive on day one.
   >
   > **The named cost, stated precisely so the ADR can weigh it.** The two places `pyfailsafe` reads
   > the clock and sleeps are the **open → half-open timing** (`circuit_breaker.py:139,142`) and the
   > **retry backoff wait** (`failsafe.py:103`) — but neither is separable from the code around it, so
   > the facade ends up owning **the entire breaker state machine and the entire retry loop**: the three
   > state classes and their transitions, failure counting, the breaker consult,
   > `record_success`/`record_failure`, the abort branch, the three callback invocations, the backoff
   > wait, and the `CircuitOpen`/`RetriesExhausted` raises. Two facts from the source force this.
   > **Counting is not separable from opening:** `record_failure` increments and trips in consecutive
   > lines (`circuit_breaker.py:124-125` — `self.current_failures += 1`, then
   > `if ... >= maximum_failures: self.circuit_breaker.open()`), and `open()` immediately stamps
   > `self.opened_at = time.monotonic()` (`circuit_breaker.py:139`), so a delegated count engages the
   > library's own clock and bypasses the injected one. A breaker configured never to open reduces
   > "delegated counting" to `self.n += 1`. **The callbacks and the wait are not separable from the
   > loop:** they are *configured* on `RetryPolicy` but *invoked* by `Failsafe.run` — the library's sole
   > entry point (`failsafe.py:59`) — at `failsafe.py:92,98,105,107`, on either side of the
   > `await asyncio.sleep(wait_for)` at `failsafe.py:103`. Owning the wait means owning that loop.
   >
   > **What the dependency still supplies is therefore exception classification and backoff computation
   > only** — `retry_policy.py`'s `should_abort` / `_is_retriable_exception` and `Backoff.for_attempt`:
   > **136 of `pyfailsafe`'s 539 lines, of which roughly 40 are non-trivial** (the exponential factor,
   > jitter, and the `max_delay` clamp). That is a severe narrowing of what the dependency is still
   > worth, and it is recorded as a cost rather than hidden: **owner** the maintainer; **revisit
   > trigger** the first time the facade has to reach into `failsafe` internals to keep a timing
   > behaviour correct, or the first behaviour that regresses on a new interpreter in the R1 matrix.
   >
   > **The costing at Step 22 is a live input, not a formality** — the same treatment R7 already gives
   > the vendored breaker's 250-line bound, and against a ~136-line (~40 non-trivial) delegable residual
   > that bound is a genuinely competitive comparator rather than a formality. If Step 22's costing
   > finds the residual smaller still — the facade having to reimplement classification or backoff
   > computation as well — that is **evidence for vendoring**, and the ADR says so instead of shipping a
   > facade that has quietly become a re-implementation.
   >
   > This is an explicit keep-with-named-cost, which is what locked decision 5 ("no silent keep, no
   > silent swap") demands. Because it deviates from letting Step 22 decide, it is **overturnable by
   > the human at checkpoint** and is flagged as **OQ12**.
3. **If fit → keep**, and record the dormancy as an accepted cost with an owner and a revisit trigger
   (trigger: the first interpreter in the declared range on which it fails to import, or the first of
   the seven behaviours to regress).
4. **If unfit → choose between:**
   - **(a) a maintained replacement**, evaluated against the `library-review` dimensions (need & fit,
     alternatives, maintenance & bus factor, adoption, license, security, transitive weight, API fit
     and exit cost, operational fit). Adding a dependency requires user approval.
   - **(b) a vendored minimal breaker** — a single module implementing exactly the seven behaviours
     above (behaviour 7 comes free on this path: a module you own can take the clock as a parameter),
     in `helpers/internal/circuit_breaker_helper.py`, with no new dependency. This is the
     default recommendation if (a) yields nothing that passes `library-review` cleanly, because the
     needed surface is small and the exit cost of a third dormant dependency is high.
5. Whichever path is taken, the alternative that was rejected is recorded with its reason.

**Acceptance Criteria**:
- [ ] The **seven** fitness behaviours exist as tests in `tests/` and run in CI, **whichever**
      implementation is chosen — they are the contract, not the library. Behaviour 7 is asserted
      **against the R24 facade**, not against `pyfailsafe`: the facade is what must accept an injected
      `clock`/`sleep`, and it does so by owning the breaker state machine and the retry loop itself
      (Ruling D). A behaviour-7
      test written against the dependency would now be a test of a known-false proposition.
- [ ] Behaviours 1–6 are executed **on every interpreter in the declared range** by the R1 matrix, and
      the matrix output is the evidence pasted into the ADR. A run on the floor alone does not
      discharge this criterion.
- [ ] **Behaviour 7's failure is recorded as executed evidence, not re-derived.** The ADR carries the
      three grepped lines (`circuit_breaker.py:139,142`, `failsafe.py:103`) verbatim as the proof that
      `pyfailsafe` has no injectable clock or sleep. A Step-22 "evaluation" that re-opens a question
      already closed by execution does not discharge this criterion.
- [ ] A written decision record exists (an ADR under `docs/decisions/`, linked from the CHANGELOG)
      naming: the option chosen, the evidence that chose it (the matrix fitness-test output for
      behaviours 1–6, and the grep above for behaviour 7), the options rejected and why, the accepted
      cost, and the revisit trigger. **Because behaviour 7 is known-failed, the ADR must specifically
      record why keep-with-wrapper beat *replace* and *vendor***, and must carry the named cost from
      the decision box above **verbatim**: the facade owns the entire breaker state machine and the
      entire retry loop — failure counting, the state transitions, the callback invocations and the
      backoff wait — and the dependency supplies **exception classification and backoff computation
      only** (`retry_policy.py`, 136 of 539 lines, ~40 of them non-trivial); owner the maintainer;
      revisit trigger the first reach into `failsafe` internals. An ADR that records a *larger*
      residual than that has failed this criterion, not rounded it. The human's answer to
      **OQ12** is recorded in the same ADR.
- [ ] If a new dependency is adopted, the `library-review` checklist is completed in the ADR and user
      approval is recorded.
- [ ] If a breaker is vendored, it is **≤ 250 lines**, has no new dependency, and reaches 100% line +
      branch coverage like everything else. **The line estimate is an input to the keep/vendor trade,
      not a foregone conclusion:** the vendored module must reproduce the whole surface R24 pins in
      `RetryConfig` (retriable and abortable exception lists, three callbacks, constant *and*
      exponential backoff, jitter) plus a breaker with a half-open state — at 100% branch coverage.
      The earlier "≤ ~150 lines" bound did not survive costing that surface. If the ADR's own estimate
      exceeds 250, that is evidence **for** keeping or replacing, and R7 says so rather than shipping
      an over-budget vendored module.
- [ ] `helpers/internal/circuit_breaker_helper.py` exposes the **same** internal interface either way,
      so R24's per-destination registry work is independent of this decision. Concretely, that
      interface is the **facade** R24 builds at Step 14: it owns `clock`/`sleep` and, inseparably from
      them, the whole breaker state machine and the whole retry loop — failure counting, the state
      transitions, the callback invocations and the backoff wait — and delegates **only** exception
      classification and the backoff computation to whatever R7 keeps, replaces or vendors underneath. The
      facade is therefore built **before** Step 22 and does not depend on Step 22's outcome — which is
      what stops a keep/replace/vendor decision sitting on the critical path to Step 26.

**Edge Cases**:
- A "fit" result that rests on the library never being exercised (the current state — H8 means the
  breaker can never open) is not fitness. The tests in criterion 1 must exercise the breaker for real.
- A "fit" result obtained only on the interpreter floor is likewise not fitness — see criterion 2.
- `str(RetriesExhausted())` is `''` and `str(CircuitOpen())` is `''`; any replacement must either carry
  a message or be wrapped by R10's `unwrap_cause` so the error is never empty.
- Vendoring is not free: it transfers maintenance to this project. The ADR records that.

---

### Group C — The public response contract and the error model

> Everything in Groups D–L returns through this contract. It lands before the protocol work so that
> each protocol fix is written against the final shape, not retrofitted onto it.

#### R8: One response envelope, returned by every protocol, with a success predicate that can fail

**Description**: `request()` currently returns three mutually incompatible shapes, and the natural
success check `if result['api_response']:` is truthy in **all three**, so it can never detect failure.
Replace them with a single documented envelope carrying an explicit boolean success predicate and an
explicit per-protocol status code. This is a breaking change, deliberately bundled with the R5 version
reset.

**User Story**: As a developer importing async-gateway, I want every protocol to return the same
response shape with one unambiguous success field, so that I can write one error-handling path instead
of three and so that my success check actually detects failure.

**The defect, precisely.** `logic/ftp.py:66` and `logic/sftp.py:60` both `return True`;
`logic/http.py:70` returns a dict. `async_gateway.py:105` annotates `response['api_response']: Dict`
while assigning `True`, and `request()` is annotated `-> Dict`. Worse, on the failure path
`logic/http.py:42` rebinds `self.response` only on success, so the `except` block at `:66` mutates the
**caller's own dict** (created at `async_gateway.py:84`, passed in at `:104`), which `:105` then
assigns into itself — `r['api_response'] is r` evaluates to `True`. And on the *success* path
`request_helper.py:93` builds its response from `kwargs.get('response', {})`, which `logic/http.py`
never passes, so the returned dict is a **fresh** one that silently drops the `url`, `payload`,
`external_call_request_time` and `error_message` that `request()` had put there.

**Acceptance Criteria**:
- [ ] A single `GatewayResponse` structure is defined once, in `async_gateway/utils/envelope.py`, as a
      `TypedDict` (or dataclass) with full annotations, and `request()` is annotated to return it.
- [ ] `request()` returns the **same key set** for every protocol — HTTP, HTTPS, SOAP, FTP, SFTP — on
      both the success and the failure path. A parametrised test asserts
      `set(result.keys()) == EXPECTED_KEYS` across all five protocols × {success, failure}.
- [ ] The envelope carries `ok: bool` as the single success predicate. A test asserts `result['ok'] is False` for every failure mode in the R10 error table, and `is True` only on success.
- [ ] The envelope carries `status_code: int`, always populated, per the mapping table in Part B.
      **`999` no longer exists**: `grep -rn "999" async_gateway/` returns zero matches.
- [ ] The envelope uses `latency` (never `tat`). `grep -rn "'tat'" async_gateway/` returns zero matches.
- [ ] `api_response` is **removed**, not re-shaped. Removal is deliberate: old code doing
      `if result['api_response']:` then fails loudly with `KeyError` instead of silently reading a
      truthy value. A test asserts the key is absent.
- [ ] The envelope is never self-referential: a test asserts `result is not result[k]` for every key
      `k`, and that `json.dumps(result)` succeeds on **every** success and failure path (today it
      raises `TypeError: Object of type RetriesExhausted is not JSON serializable` on the failure path
      because C2 stores the live exception object under `text`).
- [ ] `text` is always a `str` (`''` when not applicable) and never an exception object. A test asserts
      `isinstance(result['text'], str)` on every path.
- [ ] **Redaction (M20) — and the invariant is exactly what the redactor can deliver.** The envelope
      never carries credential material in the three places the redactor owns:
      - **Headers and cookies** — `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`,
        `X-Api-Key` values, and any `aiohttp.BasicAuth` object, are replaced with a fixed sentinel.
      - **The URL** — `url` is passed through `redact_url` before it enters the envelope: userinfo in
        the netloc (`https://user:pw@host/`) is stripped, and the *value* of any query parameter whose
        **name** is in the sensitive-name set is masked. The set is a documented module constant
        (default: `api_key`, `apikey`, `access_token`, `refresh_token`, `token`, `secret`, `password`,
        `passwd`, `signature`, `sig`, `key`, `auth`), matched case-insensitively, and extensible via
        `protocol_info['redact_query_params']`.
      - **The payload echo** — masked by **key name** against the same sensitive-name set, recursively
        to a documented depth of **4**. Below that depth, and for non-mapping payloads (`str`, `bytes`,
        a file body), the caller's own data is echoed **verbatim**, and the README says so plainly.
      A test posts a request carrying an `Authorization` header, a `?api_key=` query parameter, a
      `{'password': ...}` payload and a `Set-Cookie` response, and asserts none of the four secret
      values appears anywhere in `repr(result)`. **There is no `redact=False` opt-out** — a switch
      whose only function is to put credentials back into the envelope is not a feature this release
      ships (see "Deliberately cut sub-features").
- [ ] **A remote-status failure still carries the response.** Before a protocol raises
      `HttpStatusError` / `SoapFaultError` / `FtpStatusError` / `SftpStatusError`, it has already
      populated `status_code`, `headers`, `cookies`, `text` and `json` on the envelope exactly as it
      would on the success path, and `finalise_error` does not clear them (invariant **E11**). A test
      asserts that an HTTP **404 carrying a JSON error body** yields `ok is False`, `status_code == 404`,
      a non-empty `text`, **and** a populated `json` — the single most common way a consumer uses a
      failure response, and the case an exception-only control flow silently drops.
- [ ] **Transport helpers return data, never a response shape (FI-7's HTTP half).**
      `grep -rn "kwargs.get('response'" async_gateway/` returns zero. `make_http_request` and its
      siblings in `helpers/internal/request_helper.py` return a typed `HttpResult` (Part B); only
      `logic/*_client.py` writes into the envelope, and only `utils/envelope.py` constructs one. This
      is the criterion that owns `request_helper.py:93`'s fresh `kwargs.get('response', {})` dict —
      the defect that makes "fix H29 by changing FTP and SFTP to `return self.response`" produce three
      envelopes that are still different.
- [ ] The envelope's full key set, types, and per-protocol `status_code` mapping are documented in the
      README (R29) as the library's public contract.

**Edge Cases**:
- A protocol that has no HTTP-like headers (FTP, SFTP) returns `headers: {}` and `cookies: {}`, not a
  missing key — the key set is invariant.
- A SOAP server that returns a Fault with HTTP `200` must still yield `ok=False`; the transport status
  is not the success predicate.
- A pre-processor or post-processor raising must not corrupt the envelope; its slot carries the error
  and `ok` becomes `False`.
- An empty response body yields `text=''`, `json=None` — distinguishable from a body that was `{}`
  (see M8 / R13).

---

#### R9: Correct, timezone-honest request timestamps and monotonic latency

**Description**: The request timestamp is bound to a hardcoded `Asia/Kolkata` timezone at import time
with no override, in a package intended for publication; latency is measured with `time.time()`, so an
NTP step yields a negative value on the exact metric users dashboard.

**User Story**: As a developer importing async-gateway anywhere in the world, I want response
timestamps in an unambiguous standard form and latency that cannot go backwards, so that the numbers I
log and chart are trustworthy.

**Acceptance Criteria**:
- [ ] `request_time` in the envelope is an ISO-8601 string in **UTC** with an explicit offset
      (e.g. `2026-08-16T09:41:07.123456+00:00`). A test asserts the value parses with
      `datetime.fromisoformat` and that its `tzinfo` offset is zero.
- [ ] `TIMEZONE = 'Asia/Kolkata'` no longer exists — not as a constant, not as an import-time binding,
      not as a default. `grep -rn "Asia/Kolkata\|TIMEZONE" async_gateway/` returns zero matches. **The
      envelope timestamp is UTC and only UTC**; there is no caller-supplied display timezone and no
      `request_time_local` field (see "Deliberately cut sub-features" — MG3's defect is the *hardcoded*
      timezone, and UTC ISO-8601 with an explicit offset fully fixes it; a display-timezone feature
      would be new surface no finding asks for).
- [ ] The implementation uses `datetime.now(datetime.timezone.utc)` from the stdlib `datetime` module.
      `grep -rn "pytz" async_gateway/` returns zero matches and `pytz` is removed from the runtime
      dependency set (R6). Because no IANA name is ever resolved, **neither `zoneinfo` nor a `tzdata`
      dependency is required on any platform** — this is strictly less than Ruling 3 asked for, and it
      is called out rather than assumed (**OQ11**).
- [ ] `latency` is computed from `time.monotonic()` deltas. `grep -rn "time.time()" async_gateway/`
      returns zero matches. A test that patches the clock backwards asserts `latency >= 0`.
- [ ] `get_ist_now` is renamed to something that describes what it does (it accepts an arbitrary
      timezone); the old name is not kept as an alias — there are no consumers to keep.
- [ ] `helpers/internal/base.py:32`'s `start_time: int` annotation is corrected to `float`.

**Edge Cases**:
- **The tz-database dependency is designed out rather than handled.** `zoneinfo` would need `tzdata`
  on Windows and on slim Linux images with no system tz database; because the library resolves no IANA
  name, that failure mode does not exist and there is no platform-specific branch to test or to
  `pragma: no cover`. If OQ11 is answered by reinstating a display timezone, this edge case comes back
  and must be re-specified with it.
- A monotonic clock is process-local and has no relationship to wall time; `latency` is documented as
  a duration, never as a timestamp difference the caller can correlate with `request_time`.

---

#### R10: A real exception hierarchy, a module logger, and errors that are never empty

**Description**: All three protocol classes wrap their bodies in `except Exception`, discard the type,
and return a fabricated `status_code: 999`; the library has no logger anywhere
(`grep -rn "logging\|logger\|print(" async_gateway/` returns zero matches); and the one existing
exception class is raised in exactly one place and caught nowhere.

**User Story**: As a developer importing async-gateway, I want to catch one base exception type,
distinguish a timeout from a TLS misconfiguration from a library bug, and see the underlying cause in
the error message, so that I can retry intelligently instead of guessing at a `999`.

**Acceptance Criteria**:
- [ ] `async_gateway/utils/exceptions.py` defines a hierarchy rooted at a single public base
      `AsyncGatewayError(Exception)`, with the subclasses and status mapping in Part B. Every class
      calls `super().__init__(message)` so `Exception.args` is populated and instances survive
      pickling across `ProcessPoolExecutor`/Celery (the existing `CustomGlobalException` does not).
- [ ] `grep -rn "except Exception" async_gateway/` returns zero matches. Each protocol catches only the
      specific families it can produce (`aiohttp.ClientError`, `asyncio.TimeoutError`, `ssl.SSLError`,
      `asyncssh.Error`, `aioftp.StatusCodeError`, and the resilience library's own exceptions).
- [ ] **Programming errors propagate.** A deliberately-injected `KeyError`/`TypeError`/
      `UnboundLocalError` inside a protocol handler escapes `request()` rather than being converted
      into an envelope. A test asserts this for each of the three existing protocol classes plus SOAP.
      (This is the property whose absence concealed C6, H6 and most of the audit for the life of the
      package.)
- [ ] **`__cause__` is unwrapped recursively.** A helper `unwrap_cause(exc)` walks `__cause__` (then
      `__context__`) to the deepest non-empty message and records **both** the wrapper type and the
      cause. A test asserts that a `RetriesExhausted` raised `from ClientConnectorError(...)` yields
      an `error.message` containing the connector's text and an `error.cause` naming
      `ClientConnectorError` — **never `''`**. A naive `str(exc)` here yields the empty string
      (`str(RetriesExhausted()) == ''`), which reads as success and is worse than `999`.
- [ ] `error['message']` is never empty when `ok is False`. A test asserts
      `result['ok'] or result['error']['message']` across every failure mode in the R10 table.
- [ ] Every error carries a **stable machine-readable code** (`TIMEOUT`, `TLS`, `DNS`, `CONNECT`,
      `CIRCUIT_OPEN`, `HTTP_STATUS`, `FTP_STATUS`, `SFTP_STATUS`, `SOAP_FAULT`, `CONFIG`,
      `SERIALIZATION`, `PATH`) that is documented and does not change with the human-readable message.
- [ ] All raises chain: `grep -c "raise .* from " async_gateway/` is greater than zero and no `raise`
      inside an `except` block omits `from`. A lint rule (`B904`) enforces it.
- [ ] A module logger exists: `async_gateway/__init__.py` attaches a `logging.NullHandler()` to the
      `async_gateway` logger (library discipline — the library never configures the root logger), each
      module uses `logging.getLogger(__name__)`, and every error path logs at `error` level with a
      **redacted `extra['traceback']`** (**not** `exc_info=True` — Revision 4 / Ruling I) and structured
      `extra={}`. A test using `caplog` asserts one error record per failure carrying the redacted
      traceback string.
- [ ] **The logger leaks nothing.** A test asserts that no log record emitted during a request carries
      a password, an `Authorization` value, or a `Set-Cookie` value, using the same redaction helper as
      R8.

**Edge Cases**:
- An exception with a `__cause__` chain containing a cycle must not loop; `unwrap_cause` bounds its
  walk (depth cap) and a test covers a self-referential cause.
- An exception whose `str()` is empty *and* whose entire cause chain is empty falls back to the
  exception's class name — never to `''`.
- `asyncio.CancelledError` is a `BaseException` in Python 3.8+ and must **not** be swallowed or
  converted into an envelope; a test asserts cancellation propagates.
- The library must not add a handler other than `NullHandler`, or it will duplicate a consuming
  application's log output; a test asserts `logging.getLogger('async_gateway').handlers` contains
  exactly one handler and it is a `NullHandler`.

---

### Group D — Entry point and protocol dispatch

#### R11: Dispatch that validates its input and cannot crash on the documented call

**Description**: The entry point guards with `protocol_mapping.get(protocol.upper())` but looks up
`protocol_mapping[protocol]` unnormalised; `protocol='HTTPS'` is mapped to the same class as `'HTTP'`
with no scheme enforcement; `'SOAP'` maps to `None`; and `protocol_info=None` — the documented
"call with no protocol_info" path — crashes in the constructor.

**User Story**: As a developer importing async-gateway, I want the entry point to accept the inputs its
own documentation describes and to reject the ones it cannot serve with a clear error, so that a typo
produces a message instead of an uncaught `KeyError` from a function whose contract is to return a dict.

**Acceptance Criteria**:
- [ ] Protocol lookup is normalised **once** and the same normalised value is used for both the guard
      and the lookup. `protocol='http'`, `'Http'`, `' HTTP '` all succeed. A parametrised test covers
      lowercase, mixed case and surrounding whitespace for all five protocols.
- [ ] `protocol=None`, `protocol=''`, `protocol=123` and an unknown string each raise
      `ConfigurationError` (not `AttributeError`, not `KeyError`) with a message naming the supported
      protocols. A parametrised test covers all four.
- [ ] `protocol_info=None` and `protocol_info={}` both work for every protocol that has no required
      keys, and raise `ConfigurationError` naming the missing key for every protocol that does
      (e.g. HTTP's `request_type`). `helpers/internal/base.py:31-37` no longer reads the **raw**
      `info` on lines 33, 34 and 37 after guarding it on line 31. A test covers `None`, `{}`, and a
      dict missing each required key per protocol.
- [ ] **`protocol='HTTPS'` enforces TLS.** The URL scheme is parsed; `'HTTPS'` with an `http://` URL
      raises `ConfigurationError`, and `'HTTPS'` with a schemeless URL is upgraded to `https://`.
      `'HTTP'` accepts either scheme. A test covers all four combinations. (Today
      `grep -rn "urlparse\|scheme\|startswith('https" async_gateway/` returns zero hits and a caller
      selecting `'HTTPS'` with an `http://` URL gets silent plaintext.)
- [ ] `'SOAP'` maps to a real class (R18), not `None`. `grep -rn "'SOAP': None" async_gateway/`
      returns zero matches, and mypy no longer reports `"None" not callable`.
- [ ] The protocol registry is a typed mapping (`dict[str, type[BaseRequestClass]]`), not an untyped
      dict, so a future `None` entry is a type error.
- [ ] Input validation happens **once, at the entry-point boundary**, and internal layers do not
      re-validate what they received already-validated.

**Edge Cases**:
- A URL with an unsupported scheme (`ftp://` passed to `'HTTP'`, `file://`, `javascript:`) raises
  `ConfigurationError`. A test covers each.
- A pre-processor that mutates `response['url']` must not be able to change the protocol after the
  scheme check; the check runs against the URL actually dispatched.
- `protocol_info` supplied as a non-dict raises `ConfigurationError` rather than `AttributeError`.

---

### Group E — HTTP protocol correctness

#### R12: Request construction that dispatches on the real Content-Type and never mutates the caller

**Description**: The Content-Type → filter dispatch calls `None` for the extremely common
`application/json; charset=utf-8`, is case-sensitive on a case-insensitive HTTP header, compares
`request_type == 'GET'` case-sensitively while every other consumer lowercases, coerces booleans with
`str(True)` → `"True"`, uses `type(x) in [bool]` instead of `isinstance`, mutates the caller's payload
dict in place, and reads download-config keys with `.get()` and no default despite documenting them as
optional.

**User Story**: As a developer importing async-gateway, I want my `Content-Type: application/json; charset=utf-8` request to be encoded as JSON and my payload dict to be unchanged afterwards, so that
the library's behaviour matches its own README and my config object is safe to reuse.

**Acceptance Criteria**:
- [ ] Content-Type dispatch matches on the **media type only** (parameters stripped) and is
      **case-insensitive**. `application/json; charset=utf-8`, `Application/JSON`, and
      `APPLICATION/X-WWW-FORM-URLENCODED` all dispatch correctly. A parametrised test covers each.
- [ ] **The default is named in the spec, not left to the code.** The request-side filter table is
      exactly:

      | Media type | Filter | Wire effect |
      |---|---|---|
      | `application/json` | JSON filter | `json=` (non-GET) or `params=` (GET) |
      | `application/x-www-form-urlencoded` | form filter | `data=` as `FormData` |
      | `text/xml`, `application/soap+xml`, `application/xml` | **raw-body filter (NEW)** | `data=<body>` **verbatim**, no re-encoding, no re-serialisation |
      | anything else / absent | **the raw-body filter**, when the payload is `str`/`bytes`; the JSON filter otherwise | — |

      The old `'default': application_json_filters` entry at `helpers/internal/__init__.py:11` is
      **replaced**: routing every unknown media type to the JSON filter is what sends a SOAP envelope
      through a JSON encoder. A parametrised test covers an unknown media type with a `str` payload
      (raw pass-through), an unknown media type with a `dict` payload (JSON), and each XML type.
- [ ] **The filter functions take an explicit, typed parameter set — never `kwargs['request_type']`.**
      `application_json_filters` today does `request_type: Text = kwargs['request_type']`
      (`filters_helper.py:59`), a hard `KeyError` for any caller that does not supply one — which is
      every SOAP call. After R11, `request_type` is validated at the entry point and defaults to
      `POST` for SOAP; the filter signature declares it as a required named parameter so a missing
      value is a `ConfigurationError` at the boundary, not a `KeyError` in a helper. A test calls each
      filter with the minimum documented arguments.
- [ ] `header_filter_mapping.get(content_type)(...)` can never call `None`:
      `grep -n "mapping.get(.*)(" async_gateway/` shows no un-guarded immediate call, and an unknown
      content type is a test case, not a crash. (Today `request_helper.py:216-218` raises
      `TypeError: 'NoneType' object is not callable`, swallowed into a fake `999`.)
- [ ] The *request*-side and *response*-side content-type matching use the **same** helper. The current
      inconsistency — exact-key lookup at `helpers/internal/__init__.py:8` versus substring matching at
      `logic/http.py:58` — is itself the evidence of the bug and must not survive.
- [ ] `request_type` comparison is case-insensitive everywhere. A test asserts `"get"`, `"GET"` and
      `"Get"` all attach the payload as **query params**, never as a JSON body.
- [ ] Boolean query-parameter coercion produces `"true"`/`"false"` (what APIs expect), uses
      `isinstance(x, bool)`, and handles `None`, `list`, `datetime` and nested dicts explicitly. A
      parametrised test covers each type.
- [ ] **No caller dict is mutated.** `filters_helper.py:65` copies before modifying. A test builds a
      payload, issues a request, and asserts the caller's original dict is byte-identical afterwards
      (`payload == deepcopy_before`).
- [ ] `download_filepath` and `file_download_chunk_size` have documented defaults and are read with
      them; following the README (which documents the chunk size as *optional*) does not pass `None`
      to `iter_chunked()`. A test omits both and asserts a successful download.
- [ ] The structurally dead `if ...: pass` branch at `filters_helper.py:62` is removed.

**Edge Cases**:
- A `GET` with `http_file_upload_config` set — currently a `pass` that silently drops the file. The
  behaviour is decided explicitly (reject with `ConfigurationError`) and tested.
- A payload that is a `str` on a non-GET request must not be double-encoded.
- A header dict supplied with a lowercase `content-type` key: today
  `{'content-type': '...form-urlencoded'}` silently JSON-encodes a form payload. A test covers it.
- A payload containing a key whose value is itself a `bool` nested two levels deep.

---

#### R13: Response handling that distinguishes empty from broken

**Description**: A malformed JSON body is indistinguishable from a legitimate empty object; a
`UnicodeDecodeError` sets `error_message` but never sets `text`, which `logic/http.py:60` then reads
unconditionally, raising `KeyError` and discarding the specific diagnostic in favour of a generic
`999`; and multipart handling writes `str(bytes)` reprs into a text-mode file, truncates any part over
8192 bytes, and raises `AttributeError` when `reader.next()` returns `None`.

**User Story**: As a developer importing async-gateway, I want a malformed JSON response to be
reported as malformed rather than as an empty object, so that I do not silently process `{}` as if the
server had answered.

**Acceptance Criteria**:
- [ ] A body that is not valid JSON yields `json=None` **and** `ok=False` with
      `error.code == 'SERIALIZATION'`; a body that is a legitimate empty object yields `json={}` and
      `ok=True`. A test asserts the two are distinguishable. (Today `response_helper.py:15`
      `except ValueError: text = {}` collapses them.)
- [ ] A `UnicodeDecodeError` on the body sets **both** `text` (to the lossy-decoded value, documented)
      and `error`, and never leaves `text` unset. A test asserts no `KeyError` escapes and that the
      specific diagnostic survives into `error.message`.
- [ ] Multipart handling writes **bytes to a binary-mode file**, not `str(data)` reprs. A test posts a
      multipart response containing a PNG magic header and asserts the written file's first bytes
      equal `\x89PNG`, not `b'\\x89PNG'`.
- [ ] Multipart parts larger than the chunk size are read to completion, not truncated at one
      `read_chunk()`. A test with a part of `chunk_size * 3 + 7` bytes asserts the full length is
      written.
- [ ] `reader.next()` returning `None` terminates the loop cleanly rather than raising
      `AttributeError`. A test covers a reader that returns `None` before `at_eof()`.
- [ ] Multipart accumulation is not quadratic: the implementation appends to a list/buffer rather than
      `response_data = response_data + str(data)` inside `while True`.

**Edge Cases**:
- A response with `Content-Type: multipart/...` but a zero-part body.
- A response with no `Content-Type` header at all (today `str(None)` → `'None'`, which does not start
  with `'multipart'`, so it accidentally works — the new code must handle the absent header explicitly).
- A JSON body of literal `null` (valid JSON, decodes to `None`) must be distinguishable from a parse
  failure.

---

#### R14: HTTP transport that pools connections, always has a deadline, and bounds what it reads

**Description**: A new `ClientSession` (connector, pool, DNS cache) is constructed on **every**
request with no parameter to supply one; SFTP has no timeout at all, FTP bounds only the connect, and
two `ClientSession` constructions pass no `timeout=`; responses are read with no size cap and then
`.decode()`d into a second full copy; and a retry replays an already-exhausted async generator,
uploading a zero-byte body on attempt 2.

**User Story**: As a developer importing async-gateway into a service under load, I want connection
pooling, an enforced deadline on every call, and a bounded response read, so that a hostile or slow
endpoint cannot exhaust my ephemeral ports, pin a task forever, or OOM-kill my process.

**Acceptance Criteria**:
- [ ] `protocol_info` accepts an optional caller-supplied `session: aiohttp.ClientSession`. When
      supplied it is used and **not closed** by the library; when absent the library creates one and
      closes it. A test asserts that two requests sharing a supplied session reuse one connection
      (asserted via the tracer's `on_connection_reuseconn` event) and that the session is still open
      afterwards.
- [ ] Every `ClientSession` construction passes an explicit `timeout=`. `grep -n "ClientSession(" async_gateway/` shows no construction without it (currently `request_helper.py:29` and
      `utils/http_file_config.py:56` have none).
- [ ] A caller-set `timeout` is honoured on every protocol, including the transfer, not only the
      connect. A test per protocol asserts that a server that never responds yields
      `ok=False`, `error.code == 'TIMEOUT'`, `status_code == 504` within the configured deadline
      ± tolerance. (The circuit breaker cannot help here: `failsafe.run` reacts to raised exceptions
      and a hang raises nothing.) **On the HTTP/SOAP path the unresponsive server is the R2 fixture
      with a handler that awaits an `asyncio.Event` the test releases in teardown, and the library's
      own `timeout` is set to tens of milliseconds.** The test therefore waits only on the deadline it
      is asserting — that is the behaviour under test, not the "sleep until the async work settles"
      pattern R28 forbids, and the distinction is stated here so the two criteria are not read as
      contradicting each other.
- [ ] Response reads are **capped**. `max_response_bytes` defaults to **64 MiB (67,108,864 bytes)** and
      is overridable per call via `protocol_info['max_response_bytes']` (a positive `int`; there is no
      "disable" sentinel — a caller who needs more sets a bigger number). Exceeding it yields
      `ok=False`, `error.code == 'RESPONSE_TOO_LARGE'` rather than an unbounded allocation. A test
      streams a body over the cap and asserts the request fails without the process allocating the
      full body. Applies to **every** read path — the in-memory body read
      (`request_helper.py:128`), the streamed download (`http_file_config.py:60`), and the SOAP
      response read (R19). *Why 64 MiB:* comfortably above any realistic API/SOAP response and above
      the file sizes the README's own examples download, while small enough that a hostile endpoint
      cannot OOM a default-sized container. *(`request_helper.py:34` is deliberately **not** listed:
      it sits inside `fetch_file`, which R25 deletes as dead code — capping a function that ceases to
      exist would be an untestable criterion.)*
- [ ] Retries do not replay an exhausted body. The upload body is created by a **factory** invoked per
      attempt, not once outside `failsafe.run`. A test forces one failure then one success and asserts
      the second attempt's request body length equals the file length (today attempt 2 uploads zero
      bytes; if the server accepts it, a truncated upload is reported as success). Applies to both
      `request_helper.py:157` (the async generator) and `:185` (the file handle).
- [ ] `CHUNK_SIZE_CONSTANT` is raised to a conventional value (64 KiB) and is overridable per call.
      A test asserts the default is used when unset. (1024 costs ~100,000 await cycles on a 100 MB
      upload.)
- [ ] **The library owns the redirect loop, because R21's `allowed_schemes` has to hold on every hop.**
      aiohttp defaults to `allow_redirects=True, max_redirects=10`, which means the initial-URL scheme
      check is the *only* check a caller gets — an `https://` request that is redirected to
      `ftp://`, `file://` or plain `http://` bypasses the guardrail entirely. Therefore:
      - The library calls the transport with `allow_redirects=False` and follows redirects itself, in
        a loop bounded by `max_redirects` (default **10**, surfaced as a `protocol_info` key alongside
        `allow_redirects`, default **True**).
      - **Every** `Location` is resolved against the current URL (relative locations included) and
        re-checked against `allowed_schemes` before the next request is issued. A rejected target
        raises `ConfigurationError` with `error.code == 'CONFIG'`, naming the offending scheme and the
        hop number — it never follows and then reports.
      - Exceeding `max_redirects` yields `ok=False` with the last status in `status_code`.
      - Tests: a caller-set `max_redirects` is applied; a 302 to `ftp://` is refused **before** the
        second request is issued (asserted by the R2 test server's **recorded request count**, which
        is the fixture's replacement for a mock's call count); a 302 to a relative `Location`
        resolves correctly and is allowed; a 302 chain of `max_redirects + 1` hops fails.
      - *Rejected alternative:* raising from the `on_request_redirect` trace callback. It depends on
        aiohttp propagating an exception out of a trace hook, which is not a documented contract, and
        it leaves the redirect already issued. Owning the loop is more code but it is the only version
        that can refuse a hop rather than observe it.
      - *Scope note:* these redirect controls exist **as the vehicle for this scheme re-check**
        (orchestrator Ruling 4 / EM-H2), not as a general redirect-configuration feature. The audit's
        own Devil's Advocate retracted the claim they were otherwise traceable
        (`repo-audit-report.md:764-765`); nothing beyond `allow_redirects`, `max_redirects` and the
        per-hop check is added.

**Edge Cases**:
- A caller-supplied session that is already closed → `ConfigurationError`, not a deep aiohttp error.
- A caller-supplied session whose own timeout conflicts with `protocol_info['timeout']` — the
  per-request timeout wins; documented and tested.
- A response with a `Content-Length` above the cap is rejected **before** the body is read; a chunked
  response with no `Content-Length` is rejected mid-stream once the cap is crossed. Both tested.
- A zero-byte file upload is a legitimate case and must not be confused with the exhausted-generator
  bug; the test asserts on the file's real length, not on "non-zero".

---

### Group F — FTP protocol correctness

#### R15: FTP executes at all, defaults to FTPS, and never silently downgrades

**Description**: `verify_ssl` in `logic/ftp.py:42-48` is a function-local read before assignment, so
**both** branches raise `UnboundLocalError` — the advertised FTPS capability has never executed and
the default plain-FTP path raises on every call. Fixing that immediately exposes two latent defects on
a live socket: FTP defaults `verify_ssl` to `False` (the HTTP path correctly defaults to `True`), and
`ftp.py:43-44` reads only `ssl_context.get('ssl_context')`, a key `get_ssl_config` returns *only* when
a certificate is supplied — so `verify_ssl=True` without a certificate assigns `None` and silently
downgrades FTPS to plaintext.

**User Story**: As a developer importing async-gateway, I want FTP calls to actually run and to
protect my credentials by default, so that the advertised protocol works and my username and password
do not cross the network in the clear.

> **These three fixes land in one commit.** Fixing C6 alone makes FTP execute for the first time,
> which puts H1 (cleartext credentials by default) and M2 (silent FTPS downgrade) on a live socket.

**Acceptance Criteria**:
- [ ] `verify_ssl` is initialised from `self.verify_ssl` before the branch. A test issuing an FTP
      download against a mocked `aioftp.Client.context` asserts no `UnboundLocalError` and a populated
      envelope. `flake8` reports no `F821` in `logic/ftp.py` (today:
      `./async_gateway/logic/ftp.py:43:72: F821 undefined name 'verify_ssl'`).
- [ ] The SSL-configuration block is moved **inside** the `try`, so its failures follow the same error
      contract as everything else rather than escaping raw. A test forcing `get_ssl_config` to raise
      asserts a populated envelope with `error.code == 'TLS'`, not a bare exception.
- [ ] `verify_ssl` defaults to **`True`** for FTP, matching HTTP. A test asserts that a call with no
      `verify_ssl` key negotiates FTPS.
- [ ] **Fail closed, never downgrade.** `verify_ssl=True` with **no** certificate produces a real TLS
      context, not `None`. A test asserts that when the server does not offer TLS the call fails with
      `error.code == 'TLS'` rather than completing in plaintext. A second test asserts the value passed
      to `aioftp.Client.context(ssl=...)` is never `None` when `verify_ssl` is true.
- [ ] An explicit `verify_ssl=False` is honoured, logs a warning naming the risk, and is documented in
      the README as unsafe.
- [ ] `await client.stat(self.server_path)` no longer runs unconditionally. After a `remove`, a
      successful deletion returns `ok=True` — today the follow-up `stat` on the just-deleted path
      raises and the deletion is reported as a `999` failure, and a caller following the documented
      retry policy re-attempts a completed delete. A parametrised test covers `download`, `upload` and
      `remove`, asserting `ok=True` and the correct `protocol_details` for each.
- [ ] Both the connect **and** the transfer are bounded by the caller's `timeout` (today `ftp.py:49`
      bounds only the connect). Covered by R14's per-protocol timeout test.
- [ ] `self.command_` is validated against the R21 allowlist before `getattr`; an unknown command
      raises `ConfigurationError` rather than producing a `TypeError` swallowed as `999`.
- [ ] The FTP envelope conforms to R8: `return self.response`, never `return True`.

**Edge Cases**:
- `command` absent (`self.info.get('command', None)`) → `.lower()` on `None` raises `AttributeError`
  today; must be `ConfigurationError`. Tested.
- `auth=None` → `self.auth.login` raises `AttributeError` today; FTP requires auth, so this is
  `ConfigurationError`. Tested.
- `server_path` absent for a command that requires it.
- An FTP server that supports `AUTH TLS` for the control channel but not the data channel.
- A `remove` on a path that does not exist → `ok=False` with the server's own reply code in
  `status_code`, not a fabricated one.

---

### Group G — SFTP protocol correctness

#### R16: SFTP verifies the SSH host key, and key-based authentication is reachable

**Description**: `logic/sftp.py:32` hardcodes `known_hosts=None`, so asyncssh accepts **any** server
key on **every** session, and no consumer can turn it on because no `known_hosts` key is read from
`self.info`. `client_keys` is not passed either, so asyncssh offers the host process's ambient
`~/.ssh/id_*` identities to whatever answers.

**User Story**: As a developer importing async-gateway, I want SFTP sessions to verify the server's
host key by default and to let me pin a key explicitly, so that an on-path attacker cannot terminate
my session transparently and collect my plaintext credentials and every byte I transfer.

**Acceptance Criteria**:
- [ ] `known_hosts=None` is gone. `grep -n "known_hosts=None" async_gateway/` returns zero matches.
- [ ] The default behaviour is asyncssh's own system `known_hosts` resolution (achieved by **omitting**
      the parameter, not by passing a value).
- [ ] `protocol_info` accepts `known_hosts` (a path or an asyncssh-accepted value) and `host_key` (a
      pinned key) for explicit pinning. A test asserts a pinned key is passed through and that a
      mismatched host key fails with `error.code == 'HOST_KEY'`.
- [ ] A bypass exists but must be asked for by name: `insecure_skip_host_key_check=True`. It logs a
      `warning` naming the risk on every use. A test asserts (a) the warning is emitted, (b) the
      keyword is required — a truthy `verify_ssl=False` or similar does **not** enable the bypass.
- [ ] `client_keys=[]` is passed by default so ambient identities are never offered, and explicit
      key-based authentication is supported via `protocol_info['client_keys']`. A test asserts the
      default call passes an empty list and that a supplied key list is forwarded unchanged.
- [ ] The README documents host-key verification, pinning, the bypass, and key auth (today
      `grep -ni sftp README.md` returns **zero matches in 1,126 lines**, so a user has no way to learn
      any of this — see MG6/R29).

**Edge Cases**:
- No `~/.ssh/known_hosts` on the host (a container) → the connection fails closed with an actionable
  message naming the `known_hosts` option, not a bare asyncssh error. Tested.
- `password` **and** `client_keys` both supplied — both are offered, documented order, tested.
- A server offering only a key algorithm the client rejects → `error.code == 'HOST_KEY'` with the
  algorithm named.

> **Sequencing note:** the SFTP test fixture must be written with verification **on** from the start.
> Writing it against `known_hosts=None` and retrofitting means rewriting every SFTP test.

---

#### R17: SFTP reports the truth about operations it completed

**Description**: `remote_files` is bound only inside `if 'directory' in lstat['type']` but read
unconditionally at `self.response['files'] = remote_files`, so **any single-file target raises
`UnboundLocalError` after the transfer or mutation at `:45-54` has already succeeded** —
`mode='remove'` deletes the remote file and then reports `status_code: 999`, and a caller with the
documented retry policy retries a completed destructive operation. Separately, file metadata is
derived by string-parsing an undocumented asyncssh repr, and `self.additional_arguments.update({'recurse': True})` at `:41` permanently mutates the caller's own config dict.

**User Story**: As a developer importing async-gateway, I want a successful SFTP operation to be
reported as successful, so that my retry policy does not re-run a delete that already happened.

**Acceptance Criteria**:
- [ ] `remote_files` is initialised before the branch (or the field is omitted for non-directory
      targets). A parametrised test covers `get`, `put` and `remove` against **both** a single file
      and a directory, asserting `ok=True` for all six.
- [ ] A destructive success is reported as a success: a test performs `mode='remove'` on a single file
      and asserts `ok=True`, `status_code == 200`, and that the operation is **not** retried
      (asserted by counting calls on the mocked SFTP client).
- [ ] `self.remote_files` at `logic/sftp.py:22` — dead, mistyped, and the thing that looks like the
      missing initialisation — is deleted. Its removal is in the same commit as the fix above so it
      cannot mislead the next reader.
- [ ] File metadata comes from the typed `SFTPAttrs` fields returned by `sftp.lstat`, not from
      `{i.split(':')[0]: i.split(':')[1] for i in str(await sftp.lstat(...)).split(',')}`. A test with
      an `SFTPAttrs` whose repr contains no `':'` in a fragment (today: `IndexError`) and one with no
      `type` key (today: `KeyError`) asserts both produce a correct envelope rather than a bogus
      failure before the transfer is even attempted.
- [ ] **The caller's dict is never mutated.** `additional_arguments` is copied before
      `{'recurse': True}` is added. A test performs a directory `get` followed by a single-file
      `remove` **sharing one `protocol_info` object** and asserts the second call does not receive
      `recurse=True`, and that the caller's own
      `protocol_info['additional_arguments']` is unchanged after both.
- [ ] `connect_timeout` and `login_timeout` are passed from `self.timeout` (already computed at
      `base.py:33` and currently unused by SFTP). Covered by R14's per-protocol timeout test.
- [ ] `self.mode_` is validated against the R21 allowlist before `getattr`.
- [ ] The SFTP envelope conforms to R8: `return self.response`, never `return True`; `tat` becomes
      `latency`; `mode`, `files` and `file_stats` move under `protocol_details`.
- [ ] The class docstring at `logic/sftp.py:13` no longer says "ftp request class".

**Edge Cases**:
- A directory target under `asyncio.gather` with one shared config — the documented use case, and the
  exact shape of the cross-request state leak. Explicitly tested (see criterion 5).
- `mode` absent → `ConfigurationError`, not `AttributeError` on `None.lower()`.
- `lstat` on a symlink (`type` is neither `file` nor `directory`).
- A directory listing that is empty (`files: []` must be distinguishable from "not a directory").

---

### Group H — SOAP

> SOAP is implemented **from scratch** — `logic/soap.py` is a 0-byte file (`git cat-file -s` = 0; it
> was never populated in history), `logic/__init__.py:10` maps `'SOAP': None`, and the truthiness
> guard at `async_gateway.py:92` therefore returns `error_message: 'No Protocol Specified'`, blaming
> the caller for a protocol they *did* specify and that the package title, the README parameter table
> and the packaging description all advertise. (The README's own SOAP section reads "(upcoming)" —
> the advertisement is in the title, the parameter table, and the distribution metadata.)
>
> **Fixed scope boundary (locked human decision): no WSDL introspection and no code generation.**

#### R18: An async SOAP 1.1 and 1.2 client over the existing aiohttp layer

**Description**: Implement SOAP as a first-class protocol reusing the HTTP transport, circuit breaker,
tracer, timeout and envelope machinery already in the library: envelope construction, version-correct
headers, and response parsing.

**User Story**: As a developer importing async-gateway, I want to call a SOAP 1.1 or 1.2 endpoint with
the same `request()` signature I use for HTTP, so that I do not need a second client library for the
one protocol this one advertises but does not have.

**Acceptance Criteria**:
- [ ] `protocol='SOAP'` dispatches to a real `SoapRequest` class registered in `logic/__init__.py`.
- [ ] `protocol_info` accepts `soap_version` with values `'1.1'` (default) and `'1.2'`; any other value
      raises `ConfigurationError`. Tested.
- [ ] **Envelope construction.** The library wraps the caller's body in a well-formed SOAP envelope
      with the version-correct namespace — `http://schemas.xmlsoap.org/soap/envelope/` for 1.1,
      `http://www.w3.org/2003/05/soap-envelope` for 1.2 — and supports an optional caller-supplied
      `soap_headers` block. A test asserts the exact namespace URI per version and that supplied
      headers appear inside `<Header>`.
- [ ] The caller may supply the body as (a) a pre-built XML string, or (b) an
      `xml.etree.ElementTree.Element`. Both are tested. Dict-to-XML mapping is **not** provided
      (that is WSDL-adjacent scope).
- [ ] **Version-correct transport headers**, asserted against the headers the R2 recording
      `aiohttp.web` handler received:
      - **1.1** — `Content-Type: text/xml; charset=utf-8` **and** a `SOAPAction` header. `SOAPAction`
        is emitted **always** when the protocol is 1.1, as `SOAPAction: ""` when the caller supplies no
        action (the 1.1 spec requires the header to be present, possibly empty), and quoted when
        supplied.
      - **1.2** — `Content-Type: application/soap+xml; charset=utf-8` with the action carried as a
        **content-type parameter** (`;action="urn:..."`), and **no** `SOAPAction` header.
        Spec-conformant only; no interop escape hatch is added speculatively (see OQ5).
- [ ] **The envelope reaches the wire byte-identical.** The bytes sent as the request body are exactly
      `build_envelope(...)`'s output encoded UTF-8 — no re-serialisation, no JSON encoding, no
      whitespace normalisation. SOAP dispatches through R12's **raw-body filter** (`text/xml` /
      `application/soap+xml` → `data=`), never through the JSON filter. A test drives the call against
      the R2 `aiohttp.web` fixture, reads the raw request body its handler received
      (`await request.read()`), and asserts `body == build_envelope(...).encode('utf-8')` for both
      versions — byte-identity against a real server's view of the wire, not against a mock's record of
      what it was handed. *(This is the criterion that closes the routing hole: today
      `helpers/internal/__init__.py:11` maps `'default'` to the JSON filter, so a SOAP envelope would
      be handed to `application_json_filters` — which also `KeyError`s on `kwargs['request_type']`.)*
- [ ] **`soap_body` is defined, once and unambiguously: it is the *first element child* of `<Body>`,
      or `None`.** `None` when `<Body>` is absent, when `<Body>` is empty, or when the response body is
      empty. A `<Body>` carrying **multiple** element children returns the **first** and logs a
      `warning` naming the count — SOAP 1.1 permits multiple body entries and raising would reject
      legitimate traffic, so this is a documented narrowing, not an error. `text` carries the raw
      response string per R8. Tests: a 1.1 round-trip, a 1.2 round-trip, an empty `<Body/>` →
      `soap_body is None`, and a `<Body>` with two children → the first is returned and the warning is
      emitted.
- [ ] SOAP reuses, rather than reimplements, the HTTP layer: the same session/pooling (R14), the same
      timeout enforcement (R14), **the same capped reader (R14's `max_response_bytes`)**, the same
      circuit breaker (R24), the same tracer (R26), the same request construction path (R12) and the
      same envelope (R8). A test asserts a SOAP call populates `request_tracer`, honours `timeout`,
      and is refused with `RESPONSE_TOO_LARGE` on an over-cap body.
- [ ] `SOAP` appears in the README's protocol list with a working quickstart for both versions (R29),
      and the packaging description no longer advertises XML or redis, which do not exist anywhere.

**Edge Cases**:
- A caller supplying a body that is already a full `<Envelope>` — detected and not double-wrapped;
  tested.
- A 1.2 endpoint that returns `text/xml` anyway (non-conformant server) — parsed, with a `warning`
  logged; tested.
- A response with a `Content-Type` of `multipart/related` (MTOM) — explicitly **unsupported**, raising
  `ConfigurationError` with a clear message rather than mis-parsing. Documented as out of scope **by
  R29's SOAP criterion, which carries a doc test** — so the "documented" claim is checkable rather
  than asserted here and owned nowhere.
- An empty response body on an HTTP 202 — `ok=True`, `protocol_details['soap_body'] is None`.

---

#### R19: SOAP Faults map into the library's error contract, and XML parsing is hardened

**Description**: A SOAP Fault is a protocol-level failure that frequently arrives with HTTP 500 — and
sometimes with HTTP 200. It must set `ok=False` regardless of the transport status. And because the
library parses XML from arbitrary remote servers, the parser must not be a denial-of-service vector.

**User Story**: As a developer importing async-gateway, I want a SOAP Fault surfaced through the same
`ok`/`error` contract as every other failure, so that my one error-handling path catches it.

**XML parsing dependency — decision and justification (locked decision 1 requires this be decided):**

Runtime XML parsing uses **stdlib `xml.etree.ElementTree`**, hardened by a mandatory pre-parse guard.
No new runtime dependency is added; `lxml` stays **dev-only** (test fixtures and assertions).

*Why.* Per CPython's own XML-vulnerabilities table, `xml.etree` is **safe** against external-entity
expansion and DTD retrieval (3.7.1+) and **vulnerable** only to entity-expansion DoS
("billion laughs" / quadratic blowup) — and that class requires a `DOCTYPE`/internal entity
declaration, which a SOAP 1.1 or 1.2 envelope never legitimately carries. Rejecting a document whose
**prolog** declares a `DOCTYPE` therefore closes the entire relevant class at zero dependency cost, and
R14's response-size cap — declared as a hard dependency below — bounds the remaining input (unbounded
tree size and decompression bombs, which the DOCTYPE guard does *not* address). The rejected
alternatives:
`defusedxml` is itself dormant (last release 0.7.1, 2021) — adding a second dormant dependency to a
release whose whole point is retiring one is the wrong trade; promoting `lxml` to runtime puts a heavy
binary wheel into a pure-Python client library for a guard the stdlib can already give us. Flagged for
ratification as **OQ6**.

**Acceptance Criteria**:
- [ ] A SOAP 1.1 `<soap:Fault>` (with `faultcode`, `faultstring`, `detail`) and a SOAP 1.2
      `<env:Fault>` (with `Code/Value`, `Reason/Text`, `Detail`) are both detected and mapped to
      `ok=False`, `error.code == 'SOAP_FAULT'`, `error.message` containing the fault string/reason, and
      `protocol_details['soap_fault']` carrying the structured fault. A parametrised test covers both
      versions.
- [ ] A Fault arriving with **HTTP 200** still yields `ok=False`. A test asserts this explicitly — it
      is the case a transport-status-only check misses.
- [ ] `status_code` carries the real HTTP status (500, 200, whatever the server sent); the Fault is
      signalled by `ok` and `error`, not by a synthesised status.
- [ ] **DOCTYPE rejection, prolog-scoped — not a body-contains check.** The guard rejects only a
      `<!DOCTYPE` declaration appearing in the document **prolog**, i.e. before the root element's
      start tag. It is implemented either by scanning the prolog (everything up to the first `<` that
      begins a non-`<?`/`<!--`/`<!DOCTYPE` token) or by driving an `expat` parser with a
      `StartDoctypeDeclHandler` that raises `UnsafeXmlError`; the two are equivalent and the choice is
      the implementer's. **A substring test over the whole body is explicitly forbidden**: a Fault
      `<detail>`, a CDATA section, or a base64 field whose decoded text contains the literal word
      `DOCTYPE` — an HTML snippet echoed back inside an error payload is the everyday case — would be
      rejected as `XML_UNSAFE` on a perfectly valid response. Two tests, both required:
      1. a classic billion-laughs envelope (`<!DOCTYPE` + internal entity subset in the prolog) is
         rejected with `error.code == 'XML_UNSAFE'`, memory does not balloon, and
         `ElementTree.fromstring` was never called;
      2. a well-formed SOAP response whose `<detail>` element **contains the literal text `DOCTYPE`**
         is **accepted** and parsed normally.
      The guard is load-bearing for the entire XML-dependency decision (OQ6), so its precise form is
      part of the requirement, not an implementation detail.
- [ ] **The SOAP response read goes through R14's capped reader.** A test feeds an over-cap XML body
      and asserts `ok=False`, `error.code == 'RESPONSE_TOO_LARGE'`, **before any parse is attempted**.
      This criterion exists because the justification above leans on the byte cap to bound the input
      the parser sees; without it, R19's security argument would depend on a requirement that never
      declares it. **R14 is a hard dependency of R19** — see the requirement-ordering table in
      Dependencies.
- [ ] Malformed XML yields `ok=False`, `error.code == 'SERIALIZATION'` with the parser's position
      information preserved — never a silent `{}`. Tested.
- [ ] `grep -rn "lxml" async_gateway/` returns zero matches (dev-only usage stays in `tests/`).

**Edge Cases**:
- A Fault whose `detail` element is absent, empty, or contains nested application XML.
- A response that is valid XML but is not a SOAP envelope at all (an HTML error page from a proxy) →
  `error.code == 'SERIALIZATION'` with the first bytes of the body in the message, not a crash.
- A 1.2 Fault carrying a `Code/Subcode` chain — the full chain is preserved in
  `protocol_details['soap_fault']`.
- An XML declaration with an encoding that disagrees with the HTTP `charset` parameter.

---

### Group I — Async correctness

#### R20: No blocking I/O anywhere on an async path

**Description**: Four synchronous file operations sit lexically inside `async def` functions. Each
`open()`/`write()` blocks the entire event loop; `request_helper.py:126` blocks **once per chunk** for
the whole of a large download, and `:185` hands a *synchronous* file object to aiohttp, which then
reads it with blocking calls while the upload is in flight. In a library whose entire value proposition
is concurrency, one large transfer stalls every other in-flight request in the consuming process.

**User Story**: As a developer importing async-gateway into a service serving concurrent requests, I
want a large transfer through this library not to stall my other in-flight work, so that using the
library does not become a self-inflicted denial of service.

**Acceptance Criteria**:
- [ ] All four sites are converted:
      - `helpers/internal/request_helper.py:69` — `with open(response_file_name, 'w')` → `async with aiofiles.open(..., 'wb')` (binary, per R13).
      - `helpers/internal/request_helper.py:120,126` — `with open(..., 'wb')` and the per-chunk `read_file.write(chunk)` → `async with aiofiles.open(...)` / `await f.write(chunk)`.
      - `helpers/internal/request_helper.py:185` — the synchronous file handle handed to aiohttp → a
        streaming async body (which also satisfies R14's retry-safe body factory).
      - `utils/http_file_config.py:78` — `os.remove(local_filepath)` → `await aiofiles.os.remove(...)`.
- [ ] `grep -n "^\s*with open(\|[^a]\bopen(" ` finds no bare `open(` lexically inside any `async def` in
      `async_gateway/`. An AST-based check (a test, so it runs in CI) asserts this over the whole
      package and **fails** if a new one is introduced.
- [ ] `grep -rn "os.remove\|os.path\|shutil\." async_gateway/` finds no synchronous filesystem call
      inside an `async def`.
- [ ] A lint rule (a `flake8` plugin selection or the AST test above) bans bare `open(` inside
      `async def`, so the class cannot regress.
- [ ] A concurrency test, stated as a **counting** assertion rather than a wall-clock one: two
      coroutines run under `asyncio.gather`, one performing a mocked download of exactly **256
      chunks**, the other looping on `asyncio.sleep(0)` and incrementing a counter until the download
      finishes. The test asserts the observer completed **at least one round-trip per downloaded
      chunk (≥ 256)**. That is the scheduling-fairness property the requirement actually cares about —
      the download yields to the loop between chunks — and it is deterministic: no clock is read, no
      duration is asserted, and it satisfies R28's no-sleep-based-waiting rule. *(An earlier draft
      asserted "the loop is not starved beyond a stated bound" in wall-clock time. That is flaky in
      CI, in tension with R28, and measures the runner's load rather than the library's behaviour.
      The bound is now a ratio, and the AST criterion above remains the primary guarantee.)*

**Edge Cases**:
- `aiofiles` is **already a declared dependency and is already used correctly** in the same module at
  `:35` and `:44`, ~30 lines from the broken sites — this is inconsistency, not a missing capability,
  so no new dependency is involved.
- `aiofiles.os.remove` on a file that no longer exists — see R22.
- A very large file where the async write path must not buffer the whole file in memory (interacts
  with R14's cap).

---

### Group J — The caller-controlled capability surface

#### R21: Bounded, documented verb allowlists, and an explicit URL-trust contract

**Description**: Five sites call `getattr()` on caller-supplied verb strings with no allowlist
(`sftp.py:43`, `ftp.py:51`, `request_helper.py:31,100`, `http_file_config.py:57`). This crosses no
privilege boundary — `mode` *is* the API's documented verb, and a caller who can pass `mode='rmtree'`
can equally pass `mode='remove'` — but it is an undocumented, unbounded capability surface that fails
open on typos. Separately, the library is a gateway: the caller choosing the URL is its *purpose*, not
a defect, but it neither documents that the caller owns URL validation nor offers any opt-in guardrail.

**User Story**: As a developer importing async-gateway, I want a mistyped verb to be rejected by name
and I want the library to tell me plainly that I own URL validation, so that I neither invoke something
I did not mean nor assume the library is guarding against SSRF on my behalf.

**Acceptance Criteria**:
- [ ] Each protocol declares an explicit allowlist (an `Enum` or frozen mapping) of the verbs it
      supports, and every `getattr` call site consults it first. `grep -n "getattr(" async_gateway/`
      shows every remaining call preceded by an allowlist check in the same function.
- [ ] An unknown verb raises `ConfigurationError` naming the verb and listing the allowed values —
      **fail closed**. A parametrised test covers one unknown verb per site (5 sites).
- [ ] The allowlists are the source of the README's verb tables (R29), so documentation cannot drift
      from behaviour. A test asserts every allowlisted verb appears in the README and vice versa.
- [ ] The README carries an explicit **"You own URL validation"** section stating that
      `async-gateway` will fetch whatever URL it is given, that this is deliberate, and that callers
      accepting URLs from untrusted input must validate them (SSRF). Today
      `grep -i "ssrf\|validate\|sanitiz\|untrusted\|allowlist" README.md` returns zero hits.
- [ ] `protocol_info` accepts an optional `allowed_schemes` (default `{'http','https'}` for HTTP/SOAP)
      and rejects anything else with `ConfigurationError`. Tested. This is the *minimum* guardrail and
      is in scope; the richer opt-in guardrail (a host allowlist, a caller-supplied validator hook) is
      **deferred** — see the deferral summary.
- [ ] **`allowed_schemes` is enforced on every redirect target, not only the initial URL**
      (orchestrator Ruling 4). A guardrail that checks the first URL and then lets aiohttp follow ten
      hops unchecked is not a guardrail — the in-scope half of M21 is only true if the check holds
      per hop. The mechanism is R14's library-owned redirect loop; the criterion here is the security
      property: a test issues a request to an allowed `https://` URL whose response is a 302 to
      `ftp://evil/`, and asserts `ok=False`, `error.code == 'CONFIG'`, and that **no second transport
      call was made** (the R2 test server's recorded request count stays at 1). A second test asserts
      the same for a 302 to
      `file://`. *(Unlike the host allowlist and the validator hook, this is not new API surface — it
      is what makes the surface already being shipped honest.)*

**Edge Cases**:
- `request_type='close'` on the HTTP path resolves to `ClientSession.close(url, **filters)` →
  `TypeError`, today swallowed as `999`. With the allowlist it is a `ConfigurationError`. Tested.
- A verb that exists on the client object but is not a coroutine function.
- Case and whitespace normalisation on verbs, consistent with R11's protocol normalisation.

---

#### R22: Path containment and safe local file handling

**Description**: `grep -rn "abspath\|realpath\|normpath\|commonpath\|Path("` over `async_gateway/`
returns **zero** results. `download_filepath`, `local_filepath`, `remote_path`, `server_path` and
`client_path` all reach `open()` or the FTP/SFTP client verbatim. On a recursive directory download the
**remote server** supplies entry names, so a hostile server can emit `../` and write outside the
target. Writes use no `O_EXCL`/`O_NOFOLLOW` and no mode restriction, while the README's own examples use
fixed `/tmp/test.pdf` and `/tmp/temp.png`. The documented cleanup step raises `FileNotFoundError` when
the file is already gone, and the download path has no `try/finally`, so a partially-written file is
orphaned on every failure.

**User Story**: As a developer importing async-gateway, I want a hostile or buggy remote server to be
unable to write outside the directory I named, so that a directory download cannot become an arbitrary
file write on my host.

> **Operand order is normative (Revision 5, AGW-33).** For every transfer verb that moves a file
> between the local filesystem and a remote host, the operands are passed in the direction the
> underlying library defines for *that verb* — never one shared positional order across all of them.
> For an **upload** the order is **LOCAL SOURCE → REMOTE DESTINATION**. The three verbs this
> governs today are **FTP `upload`**, **SFTP `put`** and **SFTP `mput`**; all three currently pass
> `(remote, local)` and are therefore inverted. The download-direction verbs (FTP `download`, SFTP
> `get`/`mget`) take **REMOTE SOURCE → LOCAL DESTINATION** and are covered by the last criterion
> below. This requirement is where the operand order is stated because it cannot discharge its own
> first criterion — that every write path routes through `resolve_within(base, candidate)` before
> any write — over a call whose source and destination were never established. *(`copy`/`mcopy` move
> a file between two paths on the **same remote host**, so they are not local↔remote verbs and this
> rule does not reach them; whether they are admitted at all remains R21's allowlist decision.)*

**Acceptance Criteria**:
- [ ] A single `resolve_within(base, candidate) -> Path` helper canonicalises (`Path.resolve()`,
      symlinks followed) and asserts containment via `Path.is_relative_to`/`os.path.commonpath` before
      any write. Every write path in the library routes through it.
- [ ] A parametrised test feeds server-supplied entry names `../evil`, `../../etc/passwd`,
      `/absolute/path`, `a/../../b`, a name containing a null byte, and a symlink pointing outside the
      base — each raises `error.code == 'PATH'` and **writes nothing**. The test asserts the target
      directory is empty afterwards.
- [ ] Local writes to a caller-supplied path use `O_NOFOLLOW` and an explicit restrictive mode; a test
      pre-creates a symlink at the target path and asserts the write is refused rather than following
      it. Whether `O_EXCL` is used (refuse to overwrite) versus documented overwrite is decided
      explicitly and stated in the README; the default is **refuse to overwrite**, with an opt-in
      `overwrite=True`.
- [ ] `delete_local_file_path` is idempotent: deleting an already-absent file succeeds silently (it is
      documented as the post-processor *cleanup* step). A test calls it twice and asserts no exception.
- [ ] The download path has a `try/finally` that removes a partially-written file on failure. A test
      forces a mid-stream failure and asserts no partial file remains.
- [ ] `O_NOFOLLOW` is unavailable on some platforms; the implementation degrades to an explicit
      pre-write `lstat` symlink check with the same test coverage, and the platform difference is
      documented.

**Edge Cases**:
- A caller-supplied path that is itself a directory.
- A relative `local_filepath` — resolved against the process CWD, documented.
- Two concurrent downloads to the same path (last-writer-wins is documented; `O_EXCL` default makes
  the second fail).
- A remote entry name that is empty or `.`/`..` exactly.

---

### Group K — Transport security and resilience

#### R23: A client TLS context that is actually a client TLS context

**Description**: `helpers/internal/filters_helper.py:27` builds the mTLS context with
`ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)` — a context for a **server** authenticating
incoming clients (`PROTOCOL_TLS_SERVER`, `check_hostname=False`, `verify_mode=CERT_NONE`) — and hands
it to aiohttp as the **client** context. CPython **refuses** to build a client socket from it
(`ssl.SSLError: Cannot create a client socket with a PROTOCOL_TLS_SERVER context`), so the path fails
**closed**: this is broken functionality, not a MITM hole. The client-certificate feature, and the
FTPS path the README documents at `:997`, have therefore **never once succeeded**. Separately,
`filters_helper.py:33` returns `{'ssl': verify_ssl or True}`, which is always `True` — the documented
`verify_ssl` flag is inoperative, and the library's current TLS posture is an accident of a truthiness
bug that the obvious cleanup refactor would turn into a live switch.

**User Story**: As a developer importing async-gateway, I want client-certificate authentication to
work and `verify_ssl` to mean what it says, so that mutual-TLS endpoints are reachable and my TLS
posture is a decision rather than a side effect.

**Acceptance Criteria**:
- [ ] The context is built with `ssl.create_default_context(ssl.Purpose.SERVER_AUTH)` (or no argument),
      then `load_cert_chain(...)`. `grep -n "Purpose.CLIENT_AUTH" async_gateway/` returns zero matches.
- [ ] The implementation **asserts** `ctx.check_hostname is True` and
      `ctx.verify_mode == ssl.CERT_REQUIRED` before returning, so a future refactor cannot silently
      weaken it. A test asserts both properties on the returned context.
- [ ] The context is returned under aiohttp's supported `ssl=` key, not the deprecated `ssl_context=`.
      A test asserts the keyword actually passed to the connector.
- [ ] `{'ssl': verify_ssl or True}` is replaced by an explicit boolean/context decision. A test asserts
      `verify_ssl=False` genuinely disables verification (and logs a warning), `verify_ssl=True`
      enables it, and `verify_ssl` absent defaults to `True`.
- [ ] **The mTLS path is covered by tests**, because this fix makes client certificates connect for the
      first time ever and the path would otherwise ship untested. A test generates a throwaway
      cert/key pair, runs a local TLS server requiring client auth, and asserts a successful handshake
      through the library's own `get_ssl_config`; a second test asserts a wrong/expired client cert
      fails with `error.code == 'TLS'`.
- [ ] A certificate tuple with a missing file, an unreadable file, or a mismatched key raises
      `ConfigurationError` naming the file, not a bare `ssl.SSLError`. Tested.

**Edge Cases**:
- `certificate` supplied as a 1-tuple or a string rather than a `(cert, key)` pair.
- A PEM containing both cert and key in one file.
- A passphrase-protected key (unsupported → `ConfigurationError` naming the limitation).
- Interaction with R6: aiohttp's connector `ssl=`/`verify_ssl=` handling changed between 3.9 and 3.14.
  **R6 now lands first, at Step 1.5 in Phase 0, so R23 (Step 15) is written once against aiohttp
  3.14.x and never against 3.9.5.** There is no migration and no re-run gate. See FI-10 — both the
  earlier "must be validated together, not in separate commits" wording and its successor
  ("R23 lands first; R6 re-runs its test set") are superseded.

---

#### R24: A circuit breaker that can actually open, keyed per destination

**Description**: `helpers/internal/base.py:38` constructs `CircuitBreakerHelper` in `__init__`, and
`async_gateway.py:104` constructs a fresh protocol object per call — so every request gets a breaker
with a zeroed failure count against a threshold of `CIRCUIT_BREAKER_RETRY = 5`. The breaker is
**unreachable by construction**, while the README advertises "Has an inbuilt circuit breaker". A
breaker that looks configured and never trips is worse than none, because callers stop adding their
own. The retry defaults compound it: `CIRCUIT_BREAKER_DELAY = 0`, `CIRCUIT_BREAKER_MAX_DELAY = 0` and
`jitter=False` produce immediate, un-spaced, synchronised retries against an already-failing dependency.

**User Story**: As a developer importing async-gateway, I want the advertised circuit breaker to open
after repeated failures against **that** destination, so that a dead dependency fails fast instead of
being hammered, and one flaky host does not open the circuit for every other host I call.

> **These two fixes land together.** Hoisting the breaker to a module global without a per-destination
> registry trades a dead breaker for one flaky host opening the circuit for every destination — a worse
> outage than today's.

**Acceptance Criteria**:
- [ ] Breaker state survives across calls. A test issues `maximum_failures + 1` failing requests to the
      same destination through separate `request()` calls and asserts the last one short-circuits with
      `error.code == 'CIRCUIT_OPEN'`, `status_code == 503`, **without** reaching the transport (the R2
      test server records no further request).
- [ ] Breakers are **keyed per destination** (scheme + host + port, protocol-appropriate). A test
      opens the circuit for host A and asserts a request to host B still reaches the transport.
- [ ] The registry is bounded and does not leak: a test issuing requests to N distinct hosts asserts the
      registry size is bounded by a documented cap with LRU eviction.
- [ ] **A clock seam exists and is part of the public-to-the-library interface.** `CircuitBreakerConfig`
      carries `clock: Callable[[], float]` (default `time.monotonic`) and
      `sleep: Callable[[float], Awaitable[None]]` (default `asyncio.sleep`), and
      `get_breaker(family, host, port, config)` threads both through to the breaker and to the backoff
      computation (Part B). **Every timing assertion in this spec drives that seam** — no test
      monkeypatches a module-global `time.monotonic` or `asyncio.sleep`, and no test waits on a real
      clock. Without this, R24's half-open criterion and R28's no-sleep rule have nothing to drive.
- [ ] **The seam lives in *this library's* breaker facade, never in the resilience dependency**
      (orchestrator Ruling D). `pyfailsafe` provably has no such seam — it hardcodes `time.monotonic()`
      at `circuit_breaker.py:139,142` and `await asyncio.sleep(...)` at `failsafe.py:103` (R7, executed
      evidence) — so `helpers/internal/circuit_breaker_helper.py` is written as a **facade that owns
      the whole breaker state machine and the whole retry loop itself**, reading `clock()` and awaiting
      `sleep(d)`: failure counting, the state transitions, the callback invocations and the backoff
      wait. It delegates **only** retriable/abortable exception classification and the backoff
      *computation* to whatever R7 keeps, replaces or vendors underneath.
      A test asserts the facade never reaches a real clock: with an injected `FakeClock` and a
      recording `sleep`, a full open → half-open → close cycle completes with `time.monotonic` and
      `asyncio.sleep` untouched. **This criterion does not wait on R7** — the facade is built at
      Step 14, R7's decision lands at Step 22, and the facade's interface is identical either way.
- [ ] Half-open behaviour works: after `reset_timeout_seconds`, one trial request is allowed; success
      closes the circuit, failure re-opens it. Tested by advancing the **injected clock**, not by
      sleeping.
- [ ] Retry defaults no longer form a thundering herd: `delay` and `max_delay` default to non-zero,
      `jitter` defaults to **`True`**, and backoff is exponential by default. A test asserts, via the
      injected `sleep` seam's recorded durations, that three retries are spaced and that two
      concurrent retry sequences are not synchronised.
- [ ] Backoff selection is no longer chosen by the magic string `name == 'backoff'` while the README
      documents `name` as "Any name". A test asserts that a caller following the README's documented
      `name` value gets the documented backoff behaviour, and that `max_delay`/`jitter` are honoured.
- [ ] The circuit-breaker config is a **typed structure**, not an untyped `**kwargs` bag read with
      `.get()`. A typo (`max_failures`, `reset_timeout`) raises `ConfigurationError` naming the unknown
      key instead of being silently discarded and defaulted. A parametrised test covers three typos.
- [ ] `maximum_failures=0` is reachable (today `kwargs.get('maximum_failures', None) or CIRCUIT_BREAKER_RETRY` makes it unreachable), and `isinstance(x, int)` checks no longer accept `bool`
      or reject `float` for `delay`/`max_delay`. A parametrised test covers `0`, `True`, `1.5` and
      `"5"` for each numeric setting.
- [ ] **The caller's config dict is never mutated.** `base.py:45` currently writes `retry_policy` into
      the caller's `circuit_breaker_config` in place, polluting a reused `protocol_info` with a live
      `RetryPolicy` after the first call. A test issues two requests sharing one config object and
      asserts the caller's dict is unchanged after both.
- [ ] Whatever R7 decides, this requirement's tests are unchanged — they test the behaviour, not the
      library. The underlying implementation is **already known** not to accept a clock/sleep seam
      (R7, behaviour 7), which is precisely why the facade above owns it; a Step-22 outcome of keep,
      replace or vendor changes what sits beneath the facade and changes none of these tests. Under no
      outcome is rewriting them around a real clock permitted.

**Edge Cases**:
- A caller supplying their own breaker instance (documented as supported or explicitly not).
- Two protocols targeting the same host — same key or different? Decided: keyed by
  `(protocol_family, host, port)`, documented and tested.
- A destination that resolves to different IPs per call — keyed on the hostname, documented.
- Retries on a **non-idempotent** verb (`POST`, `remove`): the README states plainly that retry is
  caller-opt-in and that the library does not know which of your operations are idempotent. Tested only
  insofar as the documentation criterion in R29.

---

### Group L — Utilities and observability primitives

#### R25: File-transfer utilities that work, exist once, and check more than one status code

**Description**: The S3 download uses `aioboto3.client(...)`, a module-level factory **removed in
aioboto3 9.0** (the pinned version is 13.1.1, and R6 moves to 15.5.0), and passes `file_save_path=` to
`download_file`, which takes `Filename` — the function cannot ever have succeeded. It exists in **two
verbatim copies with different parameter orders** (`utils/http_file_config.py:14-38` and
`helpers/common/file_helper.py:8-31`), so a user importing the wrong one and calling positionally
writes their bucket name to a local path. The URL download checks only HTTP 403, so a 404, 500, 502 or
HTML login-redirect body is written to `local_filepath` as though it were the requested file, and any
subsequent upload step then uploads the error page to the destination.

**User Story**: As a developer importing async-gateway, I want the documented file-download utilities
to work, to exist in exactly one place, and to refuse to save an error page as my file, so that my
pre-processor does not silently hand a 404 body to my upload step.

**Acceptance Criteria**:
- [ ] `helpers/common/file_helper.py` is **deleted**, along with the now-unreferenced `fetch_file` in
      `helpers/internal/request_helper.py:13` (dead code whose removal retires the duplicate). A test
      asserts the module no longer imports.
- [ ] One `download_file_from_s3` remains, in `utils/http_file_config.py`, using the current
      `aioboto3.Session().client(...)` API and the correct `Filename=` keyword. A test with a mocked
      `aioboto3` asserts the exact call signature.
- [ ] The S3 function's parameters are **keyword-only** (`*`), so a positional-argument mistake is a
      `TypeError` at the call site rather than a bucket name written to disk. Tested.
- [ ] URL download raises on **any** non-success status, not only 403: a parametrised test covers 200,
      301-followed, 400, 403, 404, 500 and 502, asserting that only success writes a file and that
      every failure yields `ok=False` with the status in `status_code` and **no file on disk**.
- [ ] `STATUS_CODE_403 = 403` — a constant named after its own value — is replaced by a meaningful
      status check.
- [ ] The two documented README import paths that do not exist
      (`from async_gateway.utils.http_file_upload_config import ...` at `README.md:891` and `:1041`;
      the module is `http_file_config.py`) resolve, because R29 rewrites them against the real module.
      A doc test imports every symbol the README instructs the reader to import.
- [ ] `download_file_from_s3` and `download_file_from_url` are exported from a documented, stable
      location, and that location is the one the README names.

**Edge Cases**:
- S3 credentials absent → the underlying client's credential-chain error is wrapped in
  `ConfigurationError`, not swallowed.
- A 301/302 chain on the URL download — followed within R14's redirect cap, with the final status
  deciding.
- A success status with a zero-length body — written, documented as legitimate.
- Interaction with R6: the aioboto3 upgrade **forces** this fix; the S3 path fails to import otherwise.

---

#### R26: A request tracer whose results belong to one request

**Description**: `utils/request_tracer.py:13` creates **one** `results_collector` dict keyed only by
event name, attached to the `TraceConfig` object. `README.md:91` documents `trace_config` as
caller-supplied, so a reused tracer interleaves timings from unrelated concurrent requests, and stale
keys (a previous `on_request_exception`) persist into the next request's report — a successful request
annotated with a previous failure. `on_connection_reuseconn` at `:50` **overwrites** the request-start
baseline mid-request, so every subsequent metric under-reports latency on keep-alive connections, and
it stores an absolute timestamp among 13 relative deltas. `on_request_exception` at `:103` stores the
live exception object under a key named `..._message`; if it is an `aiohttp.ClientResponseError` it
carries `.request_info.headers` — the caller's `Authorization`.

**User Story**: As a developer importing async-gateway, I want the trace attached to a response to
describe **that** response, so that my latency dashboards are not silently wrong on keep-alive
connections and my traces do not carry a previous request's failure or my own credentials.

**Acceptance Criteria**:
- [ ] Trace results are stored per-request, on the aiohttp trace `context`, and collected into the
      envelope at the end of that request — never on the shared `TraceConfig`. A test issues two
      concurrent requests through **one** caller-supplied `trace_config` under `asyncio.gather` and
      asserts each envelope's `request_tracer` contains only its own events (no interleaving, no stale
      `on_request_exception` from the other).
- [ ] `on_connection_reuseconn` records a **relative delta** like its 13 siblings and does not
      overwrite the request-start baseline. A test with a reused connection asserts subsequent metrics
      are measured from the original start.
- [ ] `on_request_exception` stores a **string** (type + unwrapped-cause message, via R10's helper),
      never a live exception object. A test asserts `isinstance(..., str)` and that the value contains
      no `Authorization` header value.
- [ ] All 15 tracer callbacks carry full type annotations (they are named in the audit as the worst
      annotation offenders) and pass mypy at R27's bar.
- [ ] The tracer is compatible with the R6 aiohttp version's `TraceConfig` callback signatures; a test
      exercises every one of the 15 registered callbacks at least once.

**Edge Cases**:
- A caller-supplied `trace_config` list containing a tracer this library did not create — the library
  must not assume `results_collector` exists. Tested.
- A request that fails before `on_request_start` fires.
- Tracing disabled (`trace_config=[]`) → `request_tracer: []`, not a `KeyError`.

---

### Group M — Quality gates turned on

> **Sequencing constraint (load-bearing):** the gates go on **one per commit, each with a shrinking
> baseline-suppression file**, and `mypy`'s `ignore_errors` comes off **last**. Turning them all on at
> once against a codebase with zero tests surfaces every mypy error and 1,290 flake8 violations
> simultaneously, and the predictable response is to restore a suppression "temporarily" and lose the
> gate for good. The earliest signal that this is going wrong is a `git log` showing
> `ignore_errors = True` restored within a week of its removal.

#### R27: Linter, type checker and test runner all run, all pass, and none of them is configured to see nothing

**Description**: `-k pre_test` deselects the entire suite; `mypy ignore_errors = True` reports success
while hiding every error; `flake8` cannot start on a clean install and, once forced to run, reports
1,290 violations of which two are real signals buried in 938 quote-preference noise;
`application_import_names = async-gateway` names an illegal Python identifier so import-order checking
has been checking a fiction.

**User Story**: As the **maintainer**, I want every quality control to be capable of failing, so that
a green run means something.

**Acceptance Criteria**:
- [ ] **The suite can fail.** `-k pre_test` is gone (R2). A deliberately failing test added on a scratch
      branch causes `pytest` to exit non-zero; recorded once as evidence in the ticket work log. (Today,
      demonstrated: `collected 4 items / 2 deselected / 2 selected … 1 passed, 2 deselected … EXITCODE=0`
      — the failing test was never run.)
- [ ] **flake8 runs from a clean install and reports zero violations.** In a fresh venv:
      `pip install -e '.[dev]' && flake8 .` exits 0 with no output. The quote-preference rule is
      configured deliberately (one style chosen, formatter-enforced) rather than left to generate 938
      findings.
- [ ] `application_import_names` names a legal identifier (`async_gateway`, underscore), so import-order
      checking is actually checking this project. L15's five files pass.
- [ ] The **A005 stdlib-shadowing** finding is resolved without suppression: **three renames plus one
      new file.** `logic/http.py` → `http_client.py`, `logic/ftp.py` → `ftp_client.py`,
      `logic/sftp.py` → `sftp_client.py` are renames; `logic/soap_client.py` is **created** under that
      name by R18 (Step 20) and is never named `soap.py`, so it is not a fourth rename.
      **The three renames land at Step 4.5, in Phase 0 — not here.** Every test module, every
      traceability row and every later step in this spec is written in post-rename names; a rename at
      the end of the plan would mean twenty steps of file paths that do not yet exist (see **FI-15**).
      This criterion is therefore a *verification* at Step 24 — `flake8` reports zero `A005` and
      `grep -rn "logic/http\.py\|logic/ftp\.py\|logic/sftp\.py" .` returns nothing outside this spec
      and the audit report — not the step that performs it.
      (Alternative considered and rejected: a targeted `# noqa: A005` per module — rejected because the
      rename is uniform, costs nothing with zero consumers, and leaves no suppression to justify.
      Ratification is **OQ9**, which now blocks Step 4.5 rather than Step 24 and must therefore be
      answered in Phase 0.)
- [ ] **mypy reports zero errors with no suppression.** `ignore_errors` appears in **no** configuration
      file (`grep -rn "ignore_errors" . ` returns nothing outside this spec and the audit report), and
      `mypy async_gateway` exits 0. `ignore_missing_imports` may remain only where a third-party
      package genuinely ships no stubs, listed per-module with a comment naming the package — never
      globally.
- [ ] `async_gateway.py:104: error: "None" not callable` (the SOAP registry entry) is gone because R18
      registers a real class, not because it was suppressed.
- [ ] Any remaining suppression anywhere carries a specific rule name **and** a written justification;
      a CI step greps for bare `# noqa` / `# type: ignore` without a rule code or a `--` justification
      and fails.
- [ ] Formatting is deterministic and enforced: a formatter runs in CI in check mode and fails on
      unformatted code.

**Edge Cases**:
- Renaming `logic/http.py` changes `async_gateway.logic.http` to `async_gateway.logic.http_client`.
  There are zero consumers, and the documented entry point is `async_gateway.async_gateway.request`,
  so no compatibility alias is added (per the "delete the path you superseded" rule).
- A mypy error that is genuinely a third-party stub gap must be pinned per-module, not silenced
  globally, or the gate is back to where it started.
- The docstring lint rules must not be un-suppressed **before** R30's docstring pass lands, or the
  baseline file becomes permanent. Stated in the implementation steps.

---

#### R28: 100% line and branch coverage, enforced, with network boundaries mocked

**Description**: `tests/` is 26 lines across 3 files, one of which is not a test. Measured with the
project's own invocation, real coverage is `TOTAL 460 458 1%` with **every** `async_gateway/*` module at
**0%**, and `--cov=.` puts `setup.py` and `tests/` in the denominator so the suite credits itself for
covering its own test file. No `fail_under` exists anywhere.

**User Story**: As the **maintainer**, I want 100% line and branch coverage enforced by the test
command itself, so that a new untested branch cannot merge.

**Acceptance Criteria**:
- [ ] Coverage is measured over `async_gateway` only: `--cov=async_gateway`. `setup.py`/`pyproject.toml`
      and `tests/` are not in the denominator.
- [ ] **Branch coverage is on from Step 1, not from Step 26.** `--cov=async_gateway --cov-branch` is
      configured in `pyproject.toml` in the *scaffolding* step, before any protocol work, so that every
      subsequent commit is measured.
- [ ] **`fail_under` ratchets monotonically upward and can never be lowered** (orchestrator Ruling 1 —
      locked decision 7 means "100 at `1.0.0`", not "100 from the first commit"):
      - **Step 1** sets `fail_under` to the *then-measured* floor — whatever the suite actually covers
        at that moment, rounded **down** to the nearest integer. It is a floor, not a target.
      - **Every subsequent step** ends by re-measuring and raising `fail_under` to that step's measured
        value (rounded down). Raising it is part of the step's own commit; a step that adds code and
        does not raise the number has not finished.
      - **A CI check fails any commit that lowers it** (R1): the job reads `fail_under` from the base
        branch and from the head commit and exits non-zero if `head < base`. This is the mechanical
        part — a rule in prose is a rule that gets suspended "just for this PR".
      - **Terminal value: exactly `100` at Step 26**, with `--cov-branch` on. Not 99, not "≥ 95".
      - Why: flipping 0 → 100 at step 26 of 31 puts the one locked, non-negotiable criterion at the
        moment of maximum schedule pressure, and it is the only gate in this plan that would not
        follow its own "one gate per commit, shrinking baseline" rule.
- [ ] `pytest` exits 0 on a clean checkout with the full suite green and coverage at 100/100, and
      `pytest` exits non-zero at 99.9%.
- [ ] **No test reaches a remote host.** The wording is deliberately "remote host" and not "the
      network": after Ruling A, HTTP and SOAP are driven against a **loopback `aiohttp.web` test
      server** (R2) rather than a mocking library, and R23's mTLS handshake already used a loopback
      TLS server. FTP, SFTP and S3 stay mocked at their own client boundaries (see Part B's testing
      strategy). A CI step runs the suite with **outbound** network access disabled — `127.0.0.1`
      explicitly allowlisted — and it still passes. *(An earlier draft said "every protocol is mocked
      at its own boundary" and allowlisted exactly one loopback fixture; both were made false by
      Ruling A, and a criterion that forbids the fixture the plan mandates is unsatisfiable.)*
- [ ] **`# pragma: no cover` policy.** Permitted only for genuinely untestable lines — `if TYPE_CHECKING:`
      blocks, `@abstractmethod` bodies, and platform-guarded fallbacks that cannot run on the CI
      platform. Every pragma carries a `--` justification comment. A CI step greps for pragmas without
      a justification and **fails**. A second CI step asserts the total pragma count does not exceed
      **10**, so the policy cannot be used to buy coverage. *Why 10:* the permitted categories are
      countable — `if TYPE_CHECKING:` blocks in the handful of modules that need forward references
      (~4), the one `@abc.abstractmethod` body on `handle_request`, and the `O_NOFOLLOW` platform
      fallback in `utils/paths.py` (~2 branches). Eight is the honest estimate; 10 leaves one slot of
      headroom. Raising the ceiling requires a written justification in the PR description and is a
      reviewable event, which is the entire point of having a number.
- [ ] Tests mirror the source structure (`tests/logic/test_ftp_client.py`, etc.) and each test is
      independent and order-insensitive. **Stated as a runnable command:** `pytest -p randomly
      --randomly-seed=<n>` exits 0 for each of five committed seeds, run as a named CI step (R1). This
      requires `pytest-randomly` in the dev dependency set (R2). *(An earlier draft said "passes under
      `-p no:randomly`-free shuffled order", which names a flag by its absence and is not a condition
      anyone can execute.)*
- [ ] Every acceptance criterion in this spec that names a test has that test, and the test's name
      references the requirement id, so R28's coverage and the spec's traceability are checkable against
      each other.

**Edge Cases**:
- 100% coverage is not 100% correctness; the spec's per-requirement tests, not the percentage, are the
  quality argument. The percentage exists to stop *silent* untested code, and the ceiling on pragmas is
  what stops the percentage from being gamed.
- Async generators and `finally` blocks are common branch-coverage misses; they are named explicitly in
  the testing strategy.
- The concurrency test in R20 and the controlled-clock tests in R24 must be deterministic — no
  `sleep`-based waiting (per the project's testing rule: wait on conditions, never on the clock).
  **Every timing assertion drives R24's injected `clock`/`sleep` seam**; that seam is the reason this
  criterion is satisfiable at all, and R20's loop-fairness test is a counting assertion for the same
  reason.

---

### Group N — Documentation

#### R29: A README written for the developer who is importing this library

**Description**: Rewrite `README.md` from scratch. The current one is 1,126 lines, both of its primary
examples are **invalid Python** (missing commas between dict entries, a key with no colon —
`"certificate" ""` — and `< callable object >` pseudo-syntax that will not parse, with indentation
collapsed by an IDE auto-format), it instructs the reader to import a module that does not exist
(`async_gateway.utils.http_file_upload_config`, twice), it calls `download_file_from_s3` with a
parameter it does not have while omitting the two required ones, it documents **no SFTP section at
all** despite SFTP being fully implemented and registered, it advertises SOAP as "(upcoming)" while the
title and metadata advertise it as present, and it ships a live corporate endpoint (`api.fyndx1.de`)
**19 times** — republished inside every sdist because the README is embedded as `long_description`.

**User Story**: As a developer importing async-gateway into my project to make API calls, I want a
README that installs, shows me a working call per protocol, and tells me exactly what comes back and
what happens when it fails, so that I can be productive without reading the source.

**Acceptance Criteria**:
- [ ] The README covers, as named sections: **Install** · **Quickstart per protocol** (HTTP, FTP, SFTP,
      SOAP) · **Public API reference** (`request()`'s full signature and every `protocol_info` key, per
      protocol) · **The response envelope** (R8's full key set, types, and per-protocol `status_code`
      mapping) · **Error handling** (R10's exception hierarchy and error-code table) ·
      **Retry, timeout and circuit-breaker behaviour** (R14, R24) · **Transport security**
      (TLS defaults, host-key verification, client certificates) · **You own URL validation** (R21) ·
      **Supported Python versions** · **Versioning policy** · **Contributing** · **Changelog link**.
      **This list deliberately diverges from `.claude/rules/documentation.md` §1's required-sections
      template** (Architecture / Quick Start / *API Endpoints* / *Environment Variables* / Project
      Structure / Testing), which is written for a deployable service. This project is an importable
      library with no endpoints, no env vars, no services to start and no health check; the adaptation
      above keeps the rule's intent — install, run, public interface, configuration, testing,
      contributing — in the shape a library consumer needs. Stated here so the code reviewer reads it
      as a deliberate adaptation rather than a missed rule.
- [ ] **Every code block in the README is executable Python.** A test extracts every ```python block
      and `compile()`s it; the suite fails on a syntax error. (This is the mechanical criterion that
      makes H24 un-regressable.)
- [ ] **Every import statement in the README resolves.** A test extracts every `from async_gateway...
      import ...` line and imports it; the suite fails on `ModuleNotFoundError` or `ImportError`.
- [ ] **Every documented function call matches the real signature.** A test extracts the documented
      calls to the library's public helpers and validates them against `inspect.signature`.
- [ ] **The payload-echo disclosure is present and tested.** A test asserts the README's redaction
      section states that the payload echo is masked by key name to depth **4**, and that below that
      depth — and for non-mapping payloads — the caller's own data is echoed **verbatim**. This is the
      mechanical home for R8's redaction criterion's "and the README says so plainly" clause (E9, and
      the NFR secret-hygiene row), so that clause is a checkable test rather than a human judgement.
- [ ] `grep -c "api.fyndx1.de" README.md` returns **0**. Examples use `https://example.com/...` or a
      local mock; no third-party or corporate endpoint is named as "live and open for use".
- [ ] `grep -ci "sftp" README.md` is greater than zero, and the SFTP section documents host-key
      verification, pinning, the explicit bypass, and key auth (R16).
- [ ] The SOAP section documents both 1.1 and 1.2, the header/content-type differences, Fault handling,
      and states plainly that **WSDL introspection and code generation are not supported** — plus the
      three things a consumer hits *first*, which an earlier draft left undocumented:
      1. **The request body must be a hand-built XML string or an `xml.etree.ElementTree.Element`.**
         There is no dict-to-XML mapping (R18 declines it as WSDL-adjacent), so a caller arriving with
         a `dict` needs to know that before they write any code.
      2. **`protocol_details['soap_body']` is a raw `Element` (or `None`), not a mapping.** A consumer
         expecting a parsed dict gets an ElementTree node and must navigate it with ElementTree's own
         API; the README says so and shows one line of it.
      3. **MTOM / attachments (`multipart/related`) are not supported** and raise `ConfigurationError`
         rather than being mis-parsed. R18's edge-case list asserts this is "documented as out of
         scope"; this criterion is where that documentation actually lives, so the claim is tested
         rather than merely asserted.
      Mechanically checked the same way as the payload-echo disclosure: `tests/test_docs.py` asserts
      the README's SOAP section carries all three statements, so this is not a human-judged criterion.
- [ ] `999` does not appear as a status code anywhere in the README.
- [ ] The README is **substantially shorter** than 1,126 lines; the exhaustive HTML parameter tables are
      replaced by Markdown tables generated from, and checked against, the real defaults. (A test
      asserts every `protocol_info` key the code reads appears in the README, and vice versa — this is
      the anti-drift criterion, not the length.)
- [ ] The "Generating New Tags/Release" section names **one** file to bump (R5), not three.

**Edge Cases**:
- A README code block that is deliberately illustrative and not runnable is fenced as ```text, not
  ```python, so the compile test does not flag it — and there are as few of these as possible.
- The README is embedded as the distribution's long description, so anything in it is republished in
  every sdist; the endpoint criterion above is what makes that safe.
- Non-ASCII characters in the README are fine once R4 reads it with an explicit encoding (L16).

---

#### R30: Every module, class and public function documented and fully annotated

**Description**: Module docstrings are absent or content-free across the tree (`utils/__init__.py` and
`helpers/__init__.py` are 0 bytes; `"""Constants."""`, `"""Ftp."""`), one docstring documents a
"Sqlalchemy session object" in a library with no database, and the measured annotation coverage is 28
of 45 public functions (62%) lacking a return annotation and 22 (49%) with at least one unannotated
parameter — worst offenders being all 15 tracer callbacks, all three `handle_request()` overrides (the
library's core method), and 5 of 7 functions in `request_helper.py`.

**User Story**: As a developer reading async-gateway's source or relying on its type stubs, I want
every module to say what it is for and every public signature to be fully annotated, so that my editor
and my type checker can help me.

**Acceptance Criteria**:
- [ ] Every `.py` file in `async_gateway/` has a module docstring that states **what** the module does
      and **why** it exists (per the project's documentation rule). A test asserts, for every module:
      `len(mod.__doc__.strip()) >= 40` **and** the docstring contains at least **six** whitespace-
      separated words **and** it is not merely the module's filename (case- and punctuation-
      insensitive). *Why 40 and six words:* the two-clause "what + why" the documentation rule demands
      cannot be written shorter, and the four real offenders fail it decisively — `"""Ftp."""` is 4
      characters and one word, `"""Constants."""` is 10 and one, and the two 0-byte `__init__.py`
      files have no `__doc__` at all. A one-line "Internal." style docstring is exactly what this
      criterion is for; it must fail.
- [ ] Every public function, method and class has a docstring documenting arguments, return value and
      raised exceptions. Enforced by the docstring lint rules at R27's zero-violation bar.
- [ ] Every public signature is fully annotated — parameters and return. `mypy --strict`-equivalent
      settings for `disallow_untyped_defs` and `disallow_incomplete_defs` are enabled for
      `async_gateway` and pass. This is the mechanical, count-independent replacement for the audit's
      annotation-coverage measurement.
- [ ] No bare `dict` / `list` / `Dict` / `List` as a public return type; named typed structures
      (`GatewayResponse`, `SoapFault`, `CircuitBreakerConfig`, …) are used instead.
- [ ] `handle_request()` on the abstract base and all four overrides are annotated
      `-> GatewayResponse`.
- [ ] All 15 tracer callbacks are annotated (R26).
- [ ] The specific documentation defects are fixed: `logic/sftp.py:13`'s "ftp request class",
      `request_helper.py:88`'s Sqlalchemy parameter, `base.py:32`'s `start_time: int`,
      `logic/http.py:26`'s `List[aiohttp.TraceConfig()]` (a generic subscripted with an *instance*),
      and `async_gateway.py:97,105,108`'s annotated subscript targets (legal, never evaluated, never
      stored — deleted).
- [ ] `utils/exceptions.py`'s implicit-Optional and legacy `typing.Text` are replaced with
      `X | None` and `str` (the file is rewritten by R10 regardless).
- [ ] `typing.Text` appears nowhere: `grep -rn "typing.Text\|Text\b" async_gateway/` returns no type
      usages.

**Edge Cases**:
- Private helpers under 5 lines with obvious names may skip the docstring per the project rule; the
  test's scope is public surface plus modules.
- Test functions are documented by their names, not docstrings.
- A docstring that is present but content-free (`"""Ftp."""`) must fail the test — the criterion is
  content, not presence.

---

#### R31: Retire the Sphinx scaffolding that has never built

**Description**: `find docs -type f` returns exactly `make.bat`, `source/conf.py` and `source/Makefile`
— and **zero `.rst` files**, so there is no root document and Sphinx cannot build. The Makefile is in
`docs/source/` while setting `SOURCEDIR = source`, so it resolves to `docs/source/source`, and
`make.bat` is Windows-only: there is no working build path on the maintainer's own machine. Two dev
dependencies (`sphinx`, `sphinx-rtd-theme`) exist for a build that has never succeeded.

**User Story**: As the **maintainer**, I want the repository to contain no scaffolding that cannot run,
so that a contributor does not spend an afternoon discovering that the documented docs build has never
worked.

**Acceptance Criteria**:
- [ ] **Recommended:** `docs/make.bat`, `docs/source/Makefile` and `docs/source/conf.py` are deleted, and
      `sphinx` and `sphinx-rtd-theme` are removed from the dev dependency set. The README (R29) is then
      the library's documentation, and `docs/specs/` remains for this spec.
- [ ] **Alternative, if the EM prefers to keep an API-docs build:** an `index.rst` root document exists,
      the Makefile is at `docs/`, a POSIX build path exists, and `make -C docs html` succeeds in CI.
      If this path is chosen, the CI success is the criterion; the "delete" criterion above does not
      apply.
- [ ] Whichever is chosen, `release = '2.1'` no longer exists as an independent version claim (R5).
- [ ] The decision and its reason are recorded in the CHANGELOG (R33).

**Edge Cases**:
- Deleting `docs/` entirely would also delete `docs/specs/` — only the three Sphinx files are removed.

---

#### R32: Correct the repository's own agent-facing instructions

**Description**: The root `CLAUDE.md` "Commands (the source of truth for every agent)" block documents
`pip install -e '.[dev]'`, `uvicorn app.main:app --reload`, `pytest`, `ruff check . && mypy app`, and
`ruff format .` — for a **library** with no `app/` package, no ASGI server, and a `flake8`-based lint
toolchain. It actively misleads future contributors and every agent that reads it, and the file itself
says to replace any command that doesn't match the project's actual scripts.

**User Story**: As the **maintainer** (and as every future agent session), I want the project's own
command block to name the commands this project actually runs, so that automated work does not start
by running a command that cannot exist here.

**Acceptance Criteria**:
- [ ] The `CLAUDE.md` Commands block names the real commands: install (`pip install -e '.[dev]'`),
      test (`pytest`), lint (`flake8 .`), types (`mypy async_gateway`), format (the chosen formatter),
      build (`python -m build`). There is no "Run" command; the block says so explicitly (this is a
      library, not a service).
- [ ] The "Adding a feature" pointer to the FastAPI resource recipe is replaced with a pointer
      appropriate to this repository (adding a protocol: registry entry → protocol class → envelope
      mapping → error mapping → tests → README section).
- [ ] `grep -n "uvicorn\|app.main\|ruff\|app/" CLAUDE.md` returns no stale command references.
- [ ] **This edit requires explicit user approval before it is made** — `CLAUDE.md` is on the
      project-wide-files list in `.claude/rules/mandatory-workflow.md`. The approval is recorded in the
      ticket work log. Until approved, the requirement is blocked, not skipped.

**Edge Cases**:
- If the human declines the edit, this requirement is recorded as **rejected by the human**, not
  silently dropped, and MG7 stays open in the traceability table with that status.
- The stack overlay rule `.claude/rules/fastapi-patterns.md` is also mismatched to this repository;
  changing `.claude/rules/*` likewise requires approval and is raised in the same ask.

---

### Group O — Value-adds (proposed, not assumed)

> Locked decision 8 requires these be written as explicit numbered requirements — proposed, not
> silently added and not silently omitted — so the EM reviewer and the human can accept or cut each on
> its merits. **R1 (CI) is also a value-add** by that decision's wording, but it is specified in Group A
> because the sequencing makes it a prerequisite rather than an extra; cutting it would invalidate the
> ordering of the whole plan.

#### R33: A CHANGELOG

**User Story**: As a developer evaluating async-gateway, I want a changelog that tells me what `1.0.0`
contains and what the versioning policy is, so that I can judge whether to depend on it.

**Acceptance Criteria**:
- [ ] `CHANGELOG.md` exists, follows Keep-a-Changelog structure, and has a `1.0.0` entry.
- [ ] The `1.0.0` entry states plainly that the package was never published before, that the version
      moved **down** from the fork-inherited `2.7.3`, and why that is safe.
- [ ] It lists the breaking changes as breaking: the single response envelope, the removal of
      `api_response`, `tat` → `latency`, the FTP `verify_ssl` default flip, SFTP host-key verification
      on by default, and the `logic/*` module renames.
- [ ] It records the R7 resilience-library decision and the R31 docs decision.
- [ ] The versioning policy (semver, and what this project considers a breaking change) is stated once,
      in the CHANGELOG or the README, and linked from the other.
- [ ] A CI step fails a pull request that changes `async_gateway/` without touching `CHANGELOG.md`
      (with a documented `skip-changelog` label escape).

**Cut criterion**: if the maintainer will not keep it current, an out-of-date changelog is worse than
none. Accept only with the CI step above.

---

#### R34: LICENSE sanity check

**Description**: `LICENSE:3` reads `Copyright (c) 2022 Fynd` while the packaging metadata reads
`author='Arjunsingh Yadav'`; the initial commit contains only `.gitignore` + `LICENSE` with the whole
codebase squashed in afterwards, so the 2.x lineage is not in this repository. The MIT notice **is**
preserved verbatim, so there is no licence violation — this is provenance hygiene to settle before a
first release, not a defect that harms anyone.

**User Story**: As a developer whose company's legal review reads the LICENSE of every dependency, I
want the copyright holder and the licence text to be correct and internally consistent with the
package metadata, so that adopting this library is not a legal question mark.

**Acceptance Criteria**:
- [ ] The MIT licence text is verified byte-identical to the canonical MIT text apart from the
      copyright line. A test asserts this against a committed reference.
- [ ] The copyright line is confirmed correct **by the human** for this fork — either retained as
      `2022 Fynd` (upstream attribution preserved), extended to name both parties, or changed — and the
      decision is recorded. This is a human decision, flagged as **OQ7**.
- [ ] The `license` field in the packaging metadata, the `License :: OSI Approved :: MIT License`
      classifier, and the LICENSE file agree. A test asserts the metadata matches.
- [ ] The README states the licence and the attribution.

**Cut criterion**: none — this is cheap and it is a first-publish gate. Recommend accept.

---

#### R35: An `examples/` directory

**User Story**: As a developer importing async-gateway, I want runnable examples I can copy, so that I
am not copying from a README that has historically shipped invalid Python.

**Acceptance Criteria**:
- [ ] `examples/` contains one runnable script per protocol: `http_example.py`, `ftp_example.py`,
      `sftp_example.py`, `soap_example.py`, plus `error_handling_example.py` showing the R8 envelope and
      the R10 exception hierarchy.
- [ ] Every example is syntax-checked in CI (`python -m compileall examples/` or the R29 compile test
      extended to the directory).
- [ ] Examples run against a local mock or a public, non-corporate endpoint, and never against a
      third-party service the project does not own.
- [ ] `examples/` is excluded from the wheel and included in the sdist (or excluded from both —
      decided explicitly and asserted by a packaging test).
- [ ] Each example is ≤ ~40 lines and demonstrates exactly one thing.

**Cut criterion**: if R29's README quickstarts are mechanically compile-tested (they are), the marginal
value is modest. Recommend accept for the error-handling example at minimum, since that is the part of
the contract a README snippet demonstrates poorly.

---

## Fix-interaction warnings

These are **constraints on sequencing**, not advice. Each one describes a pair of fixes where applying
one alone produces a state that is worse, or equally broken, or silently untested. The story planner
must not split a bundled pair across stories that can land independently.

### Carried forward from the audit's own premortem

| # | Interaction | Constraint |
|---|---|---|
| FI-1 | Fixing **C6** makes FTP execute for the first time, which immediately puts **H1** (cleartext credentials by default) and **M2** (silent FTPS downgrade to `None`) on a live socket. | **R15 lands C6 + H1 + M2 in one commit.** A story that fixes only the `UnboundLocalError` is rejected. |
| FI-2 | Fixing **H28** makes client certificates connect **for the first time ever** — an untested path shipping inside a release presented as a security fix. | **R23's mTLS test criterion is not optional.** The fix and its live-handshake test land together. |
| FI-3 | Fixing **C2** with a naive `str(exc)` replaces `999` with `''`, which **reads as success** — `str(RetriesExhausted())` is `''` because pyfailsafe does `raise RetriesExhausted() from recent_exception`. | **R10's `unwrap_cause` is a hard criterion**, and the "error message is never empty" test is what proves it. |
| FI-4 | Fixing **H8** (hoisting the breaker out of per-request construction) without **M16** (a per-destination registry) trades a dead breaker for one flaky host opening the circuit for **every** destination — a worse outage than today's. | **R24 lands H8 + M16 together.** The per-destination test is in the same story. |
| FI-5 | Enabling `--cov-branch` breaks `tests/coverage_output.py:13` (`int("0.5432")` → `ValueError`) — **M27**. | **R2 deletes that script before R28 enables branch coverage.** Ordering is fixed in the implementation steps. |

### Added by this spec

| # | Interaction | Constraint |
|---|---|---|
| FI-6 | **`orjson.dumps` returns `bytes`, and `aiohttp.ClientSession(json_serialize=...)` requires a `str`-returning callable.** Migrating C1 by swapping `ujson.dumps` → `orjson.dumps` at `logic/http.py:30` breaks **every JSON request body** — and does so *after* the clean-venv import starts passing, so it will look like the migration succeeded. | **R3 wraps the default serializer** (`lambda o: orjson.dumps(o).decode()`) and asserts the emitted body in a test. The two form/JSON encode sites in `filters_helper.py` decode explicitly. |
| FI-7 | **The three envelopes are one seam defect, not three local ones.** On the *success* path `request_helper.py:93` builds its dict from `kwargs.get('response', {})`, which `logic/http.py` never passes — so HTTP already returns a **fresh** dict that silently drops `url`, `payload`, `external_call_request_time` and `error_message`. "Fixing H29" by changing FTP and SFTP to `return self.response` while leaving HTTP alone produces three envelopes that are *still* different. | **R8 is fixed at the seam** — one `GatewayResponse` builder that all four protocols and the entry point use — and the invariant-key-set test spans all five protocols × {success, failure}. Per-protocol envelope stories are rejected. **The HTTP half has a named owner:** R8's criterion "`grep -rn "kwargs.get('response'" async_gateway/` returns zero; transport helpers return a typed `HttpResult`, never a response shape", `helpers/internal/request_helper.py` in R8's file list, and the deletion performed in **Step 5**. |
| FI-8 | **Turning the docstring lint rules on before R30's docstring pass** produces a large baseline-suppression file for a class of finding that is about to be fixed wholesale — and baselines that large are the ones that become permanent. | **R30 lands before R27 un-suppresses the docstring rules.** Stated in the implementation steps as an explicit ordering. |
| FI-9 | **R16 (SFTP host-key verification) changes the shape of every SFTP test fixture.** A fixture written against `known_hosts=None` has no host key to verify and must be rewritten once verification is on. | **The SFTP test fixture is written with verification on from the start**, before R17's operation tests are written against it. |
| FI-10 | **R6 (aiohttp 3.9 → 3.14) sits underneath R14, R23 and R26.** The connector `ssl=`/`verify_ssl=` handling, the `TraceConfig` callback signatures and the timeout API all differ across that range — and, more sharply, **the whole HTTP/SOAP test architecture is built on whichever aiohttp is installed when it is written.** Writing it against 3.9.5 and upgrading later means every one of those tests is re-validated at the upgrade, at the point of maximum schedule pressure, against a `fail_under` ratchet that cannot be lowered. **This is the interaction that produced the plan critique's Critical:** the deferral was invisible until `aioresponses` turned it from a re-run into a rewrite. | **Dissolved, not sequenced: R6 lands at Step 1.5, in Phase 0, before any test exists** (Ruling B). R14 (Step 13), R23 (Step 15), R25 (Step 18) and R26 (Step 19) are then *written once*, against the transport they ship on. There is no migration commit, no re-run gate, and nothing written twice. **Both earlier formulations are superseded** — the one-commit bundle (Revision 1) and the "R23/R26 first, R6 re-runs their test sets" ordering (Revision 2). The Revision-2 rationale ("upgrading first would migrate three APIs with no test able to detect a mistake") does not survive contact with two facts: those three surfaces are **rewritten**, not migrated, so there is no legacy code to break; and on aiohttp 3.14.3 `verify_ssl`/`ssl` are still present on `_request` and `TCPConnector.__init__` and the `TraceConfig` callbacks are intact, so the interval between Step 1.5 and Step 19 is not a broken window. |
| FI-11 | **R6 (aioboto3 13.1.1 → 15.5.0) and R25 must not disagree about which API the S3 code targets.** `aioboto3.client(...)` was removed in 9.0. With R6 now at Step 1.5, the upgrade lands ~17 steps *before* R25. | **R25 (Step 18) is written once, against aioboto3 15.x, and no test exercises the S3 path before it.** The upgrade does not regress a working path: `download_file_from_s3` is already non-functional today — H15 shows it passes `file_save_path=` to `download_file`, which takes `Filename=` — so after Step 1.5 it fails for one more reason and is fixed once, correctly, at Step 18. **The one thing this row does *not* assert is that a bare `import aioboto3` still resolves on 15.x** (`aioboto3.client` is resolved at call time, inside the function, not at import) — that was **not executed** from this spec's position, and per the standing constraint under Assumptions it is therefore not closed by reasoning. **Step 1.5's gate is the clean-venv submodule import**, which turns red immediately if any module-level import breaks; if it does, R25's S3 call-site repair is pulled forward into Step 1.5 as the minimum needed to keep Phase 0 green, and the rest of R25 stays at Step 18. The earlier claim that "after the upgrade the S3 path does not even import" is retired as unverified. |
| FI-12 | **The security baseline moves under this release.** The audit's "38 vulnerabilities in 3 packages" counted `aiohttp`, `orjson` and `requests`; R3 removes `requests` and R6 changes the other two. A security gate configured as a delta against 38 will mis-report. | **R6's criterion is "zero Critical/High advisories in the *resolved* dependency set"**, an absolute condition, never a count delta. The scan now runs at **Step 1.5**, so the advisory position is clean from Phase 0 onward rather than from Step 21. |
| FI-13 | **R4 adds `py.typed`; R27 makes mypy clean.** Publishing the marker before the type checker is clean exports this library's type errors to every downstream consumer's build. | **`py.typed` is added only after R27's mypy criterion is green** — the last packaging step, not the first. |
| FI-14 | **R11's HTTPS scheme enforcement can be defeated by a pre-processor.** Pre-processors run before dispatch and receive the mutable response dict containing `url`. | **The scheme check runs against the URL actually dispatched**, after pre-processors, not against the caller's original argument. Tested. |
| FI-15 | **The `logic/*.py` → `logic/*_client.py` rename cannot land late.** Every test module in this plan is named `tests/logic/test_http_client.py` / `test_ftp_client.py` / `test_sftp_client.py` (created in Phase 2), R28 requires tests to mirror the source structure, and the traceability rows for R3, R13 and R14 name `logic/http_client.py` as the file they modify. A rename in Phase 6 means twenty steps of work written against filenames that do not exist yet, then a rename commit that touches every one of them — the largest, least reviewable diff in the plan, landing at the point of maximum schedule pressure. | **The rename is Step 4.5, in Phase 0**, immediately after the `orjson` migration and before any protocol work. One set of names holds from Step 5 onward. R27's A005 criterion becomes a *verification* at Step 24, not the step that performs the rename. **OQ9 (ratify the rename) therefore blocks Phase 0**, not Phase 6, and must be answered before implementation starts. `logic/soap_client.py` is created under its final name by R18 and is never renamed. |
| FI-16 | **R21's `allowed_schemes` and R14's redirect handling are one fix, not two.** Checking the scheme of the initial URL while aiohttp transparently follows up to ten redirects means the guardrail is enforced on the one URL the caller already chose and on none of the ones an attacker chooses. | **R14's library-owned redirect loop and R21's per-hop scheme check land in the same story (Step 13).** A story that surfaces `max_redirects` without the per-hop check, or asserts the per-hop check without owning the loop, is rejected. |

**Earliest signals that this plan is going wrong** (carry these into the ticket work log):
1. The first `pytest` after the gates go on exits non-zero on a **`pytest-asyncio` tooling error** rather
   than a test failure — that means R2 was skipped and the plan was never executed end-to-end.
2. `git log` shows `ignore_errors = True` restored within a week of its removal — the gates were turned
   on all at once and the baseline discipline was abandoned.
3. A protocol story merges with `return True` still in it — R8 was treated as documentation rather than
   as the contract.
4. **Step 1's fixture check is skipped, or the dev set acquires an aiohttp-mocking library.** That one
   command against the *target* aiohttp is the entire guard against the plan critique's Critical
   recurring one layer down; skipping it as "obviously fine" is how the first one survived two reviews.
5. **Step 1.5 slips past Step 3.** If CI is standing up against the un-upgraded set, the test
   architecture is again being built on a transport the release replaces — the exact shape of FI-10.

### Said out loud: C7 now closes in Phase 0, and the deferral that was disclosed here is reversed

Revision 2 disclosed, at length, a deliberate trade: **C7** — `aiohttp~=3.9.5`, a range that admits
exactly one version forever, carrying 35 advisories — would stay open until **Step 21 of 31**, so that
the three surfaces the 3.9 → 3.14 migration touches would already be correct and tested before the
upgrade landed.

**That trade is withdrawn.** The plan critique executed the two things the disclosure rested on and
both came back against it: the migration risk being hedged is small (on aiohttp 3.14.3, `verify_ssl`
and `ssl` are still present on `_request` and `TCPConnector.__init__`; the `TraceConfig` callbacks are
intact), while the risk the deferral *created* was project-stopping — twenty steps of test
architecture built on a transport the release then replaces, revalidated at the point of maximum
schedule pressure against a `fail_under` ratchet that cannot be lowered. The disclosure is not
rewritten to describe a smaller version of the same trade, because the plan no longer makes it.

**C7 is remediated at Step 1.5, in Phase 0** (R6, Ruling B). The three surfaces are *rewritten* by R14,
R23, R25 and R26 against the upgraded set, so nothing is migrated blind and nothing is written twice.
The one discipline worth carrying forward from the old disclosure: **no story after Step 1.5 may merge
while the dependency set is un-upgraded**, and R6's advisory scan ("zero Critical/High in the resolved
set") is the gate.

What the withdrawn disclosure got right, and is worth keeping on the record: nothing is published
during the delivery window (locked decision 4; A1/OQ4 confirm PyPI 404s) and there are zero consumers,
so C7's advisories were never *reachable* by anyone. The deferral's flaw was never its security
exposure — it was the schedule bomb hidden underneath it.

---

## Rollback and reversal

`.claude/rules/risk-classification.md` classifies this work **high** (dependency upgrades, security
controls, a change touching most files in the tree) and requires a stated reversal plan. Here it is.

**Everything in this release is revertible except one step, and that step is manual.**

- **The unit of reversal is the `v1.0.0` tag.** The release lands as a sequence of commits on a branch,
  each a single step from the implementation-steps list, each independently green. Reverting the whole
  release is `git revert` of the merge (or resetting the release branch); reverting one step is
  `git revert` of that step's commit. Because every step ends with a green gate, a bisect between two
  tags is meaningful — which it is not today, since there are no tags at all (H22).
- **The repository is the only artifact.** No service is deployed, no container is published, no
  migration is applied, no data is written anywhere. The DevOps gate covers CI; the Observability gate
  covers the library's own logging. There is nothing running to roll back.
- **The genuinely irreversible step is a PyPI upload — and it is out of scope.** Once `1.0.0` is
  uploaded under this name, the version can only go forward (a PyPI filename can never be reused, even
  after deletion), R5's `2.7.3 → 1.0.0` reset becomes impossible, and R8's envelope replacement stops
  being free. **Nothing is published until a human runs the upload**, which is locked decision 4 and
  is gated again by **OQ4**. Until then every "breaking change is free" argument in this spec remains
  true and every change remains a `git revert` away.
- **Two edits reach outside `async_gateway/` and both are gated on a human.** `CLAUDE.md` and
  `.claude/rules/fastapi-patterns.md` (R32 / OQ8) are project-wide files; they are edited only after
  explicit approval and are reverted like any other commit.
- **Residual risk after reversal.** Reverting restores a package that does not import from a clean
  install (C1), whose FTP path raises on every call (C6), and whose SFTP path accepts any host key
  (C4). *Reverting this release is not a safe state* — it is only a **known** state. The correct
  response to a defect found late is a forward fix through the defect loop, with the revert reserved
  for a release that cannot be made green at all. Stated so nobody reads "revertible" as "harmless to
  revert".

---

## Deferral summary

**No Critical or High finding is deferred.** One Medium is *partially* deferred, and it is listed here
so the EM reviewer and the Devil's Advocate can challenge it rather than discover it in the table.

| Finding | Deferred portion | In-scope portion | Justification |
|---|---|---|---|
| **M21** (SSRF: the library neither documents that the caller owns URL validation nor offers an opt-in guardrail) | The **richer opt-in guardrail** — a caller-supplied **host allowlist** and a caller-supplied **validator hook**. | R21 delivers the **documentation** ("You own URL validation", an explicit README section), the **minimum guardrail** (`allowed_schemes`, defaulting to `{http, https}`, rejecting everything else), **and the per-redirect-hop enforcement of that guardrail** (R14's owned redirect loop). | The documentation half is the actual finding: the report itself judges that "the caller chooses the URL is this library's *purpose*, not a defect." The two deferred items are **new API surface**, not defect fixes, and building them in the same release that is already reshaping the public contract adds surface no requirement demands (§2a.5 rung 6: no flexibility no requirement asks for). **The redirect-target re-check was previously in this deferred list and has been moved in scope** (orchestrator Ruling 4): it is not a new feature — it is what makes the in-scope half *true*, since a scheme check that only sees the initial URL is not enforcement. Revisit trigger for what remains deferred: the first consumer request for it, or the first time this library is used to fetch a URL derived from untrusted input in a project that cannot validate upstream. |

Everything else in the 89-row inventory is mapped to a requirement that fixes it in `1.0.0`.

### Deliberately cut sub-features

Three sub-features inside otherwise-necessary requirements were scope creep — each carrying a
100%-branch-coverage cost for behaviour no finding asks for. Two are cut; the third is kept, but
narrowed to the single job that justifies it. **No finding is orphaned by these cuts**, and each cut is
recorded here rather than performed silently.

| Cut | Was in | Finding it appeared to serve | Why it is cut | Where the finding is still fixed |
|---|---|---|---|---|
| `protocol_info['timezone']` + the `request_time_local` envelope field | R9 | MG3 (`TIMEZONE = 'Asia/Kolkata'` hardcoded and import-time-bound) | MG3's defect is the **hardcoded** timezone, and UTC ISO-8601 with an explicit offset fixes it completely. A caller-supplied *display* timezone is a formatting convenience the caller can perform in one line on a value the envelope already carries — and it drags in IANA resolution, an invalid-name error path, and the `tzdata` platform branch, all of which must reach 100% branch coverage. | **R9**, unchanged: no `TIMEZONE` constant, no import-time binding, UTC-only `request_time`. It also removes the `zoneinfo`/`tzdata` dependency question entirely (OQ11). |
| `redact=False` opt-out | R8 | M20 (no redaction discipline on the envelope) | The switch's only function is to put credential values back into the returned envelope and, via R10's logger, into the caller's logs. It is a documented footgun in a release whose thesis is that the library's defaults should be safe; and the "opt-out enabled" path needs its own tests to hit 100% branch coverage — tests that assert secrets *are* leaked. | **R8's redaction criterion**, which is now unconditional and — per EM-M11 — specified precisely enough (headers, cookies, `auth`, URL userinfo, sensitive-named query parameters, key-name-masked payload to depth 4) that invariant E9 is exactly what the redactor delivers. |
| Redirect controls (`allow_redirects`, `max_redirects`) — **KEPT, narrowed** | R14 | Nothing: the audit's own Devil's Advocate **retracted** the claim these traced to (`repo-audit-report.md:764-765`) | As a general redirect-configuration feature they are untraceable creep. | **Kept solely as the vehicle for EM-H2 / Ruling 4's per-hop `allowed_schemes` re-check** (R21, FI-16). The library must own the redirect loop to *refuse* a hop rather than observe it; `allow_redirects` and `max_redirects` are the two knobs that loop necessarily has. Nothing further is added. |

---

## Traceability: finding → requirement

**89 finding rows** — 7 Critical + 29 High + 28 Medium + 17 Low = **81 numbered findings**, plus the 8
`MG` ids this spec assigned to the audit's unnumbered Medium prose block (see the ID reconciliation
note). The Critical table additionally carries **one placeholder row for the vacated `C5`**, so the
tables contain **90 physical rows and 89 findings**. The placeholder is deliberate: `C5` was vacated
(not renumbered) when the Devil's Advocate reclassified it to H28, and an empty row is what stops a
reader — or a `grep` — concluding the id was lost.

### Critical (7)

| Finding ID | Sev | One-line summary | `file:line` | Requirement(s) | Notes |
|---|---|---|---|---|---|
| C1 | Critical | `ujson` imported at runtime, declared nowhere — the published package is unimportable | `logic/http.py:12`, `helpers/internal/filters_helper.py:8`, `helpers/internal/response_helper.py:5` vs `requirements.txt` | **R3**, R1 | First failing chain is `async_gateway.py:6` → `logic/__init__.py:3` → `ftp.py:7` → `helpers/internal/__init__.py:3` → `filters_helper.py:8`. A CI check importing only the 0-byte top-level package would not catch it — R1's criterion imports a submodule. FI-6 applies. |
| C2 | Critical | Blanket `except Exception` in all three protocol classes; fails open, logs nothing, returns fake `999` | `logic/http.py:65`, `logic/ftp.py:68`, `logic/sftp.py:62` (AST-verified as the only three) | **R10**, R8 | Auto-Critical (error suppression that hides failures) and may not be downgraded. FI-3: do not use `str(exc)`. This is the mechanism that concealed C6, H6 and most of the audit. |
| C3 | Critical | Blocking synchronous file I/O on async request paths | `helpers/internal/request_helper.py:69,120,126,185`; `utils/http_file_config.py:78` | **R20** | Auto-Critical (blocking I/O on an async path). `aiofiles` is already a declared dependency used correctly ~30 lines away at `:35`/`:44`. |
| C4 | Critical | SFTP disables SSH host-key verification, hardcoded, with no opt-out | `logic/sftp.py:32` | **R16** | Also passes no `client_keys`, so ambient `~/.ssh/id_*` identities are offered to whatever answers. FI-9 applies to the test fixture. |
| — | — | *(C5 vacated — reclassified to H28)* | — | — | Retained as an empty slot so nothing citing C5 dangles. |
| C6 | Critical | FTP raises `UnboundLocalError` on every call; FTPS is unreachable code | `logic/ftp.py:42-48` | **R15** | Confirmed by `flake8 F821` and a control-flow repro; both branches fail. FI-1: lands with H1 and M2. |
| C7 | Critical | `aiohttp~=3.9.5` — no patched version exists inside the declared range, ever | `requirements.txt:1` | **R6** | Structural, not advisory-dependent: 3.9.5 is the last release in the 3.9 line, so the range admits exactly one version permanently. FI-10, FI-11, FI-12 apply. **Remediated at Step 1.5, in Phase 0** (orchestrator Ruling B, closing the plan critique's Critical). The earlier plan deferred this to Step 21 of 31; that trade is withdrawn — see "Said out loud: C7 now closes in Phase 0". |
| C8 | Critical | `-k pre_test` makes the test suite structurally incapable of failing | `setup.cfg:17`, `tests/test_pass.py:1-6` | **R2**, R27 | Demonstrated: a genuinely failing test was deselected and the run exited 0. A permanent silent gag on all future tests. |

### High (29)

| Finding ID | Sev | One-line summary | `file:line` | Requirement(s) | Notes |
|---|---|---|---|---|---|
| H1 | High | FTP transmits credentials in cleartext by default (`verify_ssl` defaults `False`; HTTP defaults `True`) | `logic/ftp.py:24`; documented as intended at `README.md:996` | **R15**, R29 | Latent behind C6 today; live the moment C6 is fixed. FI-1. |
| H2 | High | SFTP raises *after* completing the operation — destructive successes reported as failures and then retried | `logic/sftp.py:40,58` | **R17** | `mode='remove'` deletes the file, then reports `999`. `self.remote_files` at `:22` (L6) looks like the missing initialisation and will mislead the fixer. |
| H3 | High | SOAP is advertised in the title, parameter table and packaging description, and does not exist | `logic/soap.py` (0 bytes), `logic/__init__.py:10`, `async_gateway.py:92` | **R18**, R19, R11 | The README's own SOAP section reads "(upcoming)"; the advertisement is elsewhere. Packaging also advertises XML and redis, which exist nowhere (R18). |
| H4 | High | Lowercase protocol passes the guard then crashes with an uncaught `KeyError` | `async_gateway.py:92` vs `:103` | **R11** | The `.upper()` in the guard is the only signal lowercase is accepted, and it lures callers into the crash. `protocol=None` raises `AttributeError` at `:92`. |
| H5 | High | `protocol_info` omitted crashes in the constructor — the documented call path | `helpers/internal/base.py:31-37` | **R11** | Guards `info` on line 31 then uses the **raw** `info` on lines 33, 34 and 37. |
| H6 | High | `protocol='HTTPS'` does not enforce TLS | `logic/__init__.py:8-9` | **R11** | `grep -rn "urlparse\|scheme\|startswith('https"` → zero hits. FI-14 applies. |
| H7 | High | Response dict becomes self-referential on the failure path (`r['api_response'] is r`) | `logic/http.py:42,66`; `async_gateway.py:84,104,105` | **R8** | `json.dumps(result)` fails with `TypeError: Object of type RetriesExhausted is not JSON serializable` (the C2 exception object), earlier than the circular-reference error the draft predicted. The same aliasing leaves `error_message` empty on every network failure. |
| H8 | High | The circuit breaker is constructed per request and can therefore never open | `helpers/internal/base.py:38`; `async_gateway.py:104` | **R24** | README advertises "Has an inbuilt circuit breaker". FI-4: must land with M16. |
| H9 | High | Retries replay an exhausted body — attempt 2 uploads zero bytes | `helpers/internal/request_helper.py:157`, `:185` | **R14** | If the server accepts it, a truncated upload is reported as success. |
| H10 | High | No timeout at all on SFTP; partial on FTP; none in two `ClientSession` constructions | `logic/sftp.py:28-32`; `logic/ftp.py:49`; `request_helper.py:29`; `http_file_config.py:56` | **R14**, R15, R17 | `base.py:33` already computes `self.timeout` and SFTP never uses it. The breaker cannot help: a hang raises nothing. |
| H11 | High | A new `ClientSession` per call discards connection pooling entirely | `logic/http.py:36` | **R14** | Fresh connector, pool and DNS cache per request; no parameter to supply one. |
| H12 | High | Unbounded response reads, plus a quadratic multipart accumulator | `request_helper.py:128`, `:34`, `:75`; `http_file_config.py:60` | **R14**, R13 | `.decode()` allocates a second full copy (~3× body resident). |
| H13 | High | *Pointer only* — down-rated to Medium by the adversarial pass; see **M25** | — | **R21** (via M25) | Retained as an id so nothing citing H13 dangles. The 3-1 lane majority for Medium was restored. |
| H14 | High | Content-Type dispatch calls `None` for `application/json; charset=utf-8` | `helpers/internal/request_helper.py:216-218`; mapping at `helpers/internal/__init__.py:8` | **R12** | Case-sensitive on a case-insensitive header, so a lowercase `content-type` form header silently JSON-encodes. The response side at `http.py:58` uses substring matching — the inconsistency is the evidence. |
| H15 | High | S3 download broken twice over, in two duplicated copies with different parameter orders | `utils/http_file_config.py:30,36`; `helpers/common/file_helper.py:23,29` | **R25** | `aioboto3.client(...)` removed in 9.0; `file_save_path=` should be `Filename=`. FI-11: R6 forces this. |
| H16 | High | Only HTTP 403 is checked, so 404/500/502/login-redirect bodies are saved as the requested file | `utils/http_file_config.py:60,68-69` | **R25** | Any subsequent upload step then uploads the error page to the destination. |
| H17 | High | The tracer's results collector is shared across all requests using one `TraceConfig` | `utils/request_tracer.py:13`; documented as caller-supplied at `README.md:91` | **R26** | Stale keys persist: a successful request annotated with a previous failure. |
| H18 | High | `mypy` reports success while hiding every error | `setup.cfg:12-14` | **R27** | Removing only `ignore_errors` turns `Success: no issues found in 22 source files` into a real error list — the report says 40 in the finding body and 44 in its summary (see **OQ2**); R27's criterion is count-independent. |
| H19 | High | `flake8` cannot start from a clean install; once forced, 1,290 violations bury two real signals | `requirements-dev.txt:5`; `setup.cfg:10` | **R2**, R27 | `flake8-import-order==0.18.2` needs `pkg_resources` (absent from bare 3.12+, removed in setuptools ≥ 81); only `setuptools<81` gets it to run. `application_import_names = async-gateway` is an illegal identifier. Real signals: `F821` (C6) and `A005` (`logic/http.py` shadows stdlib `http`). |
| H20 | High | Zero real test coverage; the suite credits itself for covering its own test file | `tests/` (26 lines, 3 files); `setup.cfg:17` | **R28** | Measured `TOTAL 460 458 1%`, every `async_gateway/*` module at 0%. No `fail_under` anywhere. |
| H21 | High | No CI of any kind | `.github` does not exist | **R1** | `README.md:1117` asks contributors to run the tools on the honour system; H18, H19 and C6 are the evidence that this does not happen. |
| H22 | High | Four mutually contradictory version claims and zero git tags | `setup.py:24` (2.7.3); `docs/source/conf.py:26` (2.1); branch `v1.0rc-1`; `git tag` empty; `__init__.py` 0 bytes | **R5** | No commit is attributable to 2.7.3, so a released version cannot be bisected against a bug report. |
| H23 | High | Exact `==` pins in a published library become hard constraints on every consumer's resolver | `requirements.txt` (`aioboto3`, `pyfailsafe`, `pytz`, `aioftp`, `asyncssh`); `setup.py:35` | **R6** | `pytz==2024.1` freezes consumers on 2024 timezone rules. Consumers also cannot patch C7 themselves. |
| H24 | High | Both primary README examples are invalid Python, and a documented import path does not exist | `README.md:139-206`, `:891`, `:985-1033`, `:1041`, `:1062` | **R29**, R25 | Missing commas, a key with no colon (`"certificate" ""`), `< callable object >` pseudo-syntax. `async_gateway.utils.http_file_upload_config` does not exist (the file is `http_file_config.py`). R29's compile + import tests make it un-regressable. |
| H25 | High | Published metadata points at another organisation's tarball; licence attribution unresolved | `setup.py:31` vs `:30`; `LICENSE:3` vs `setup.py:25` | **R5**, R34 | Severity revised twice and settled as **effectively Low/Medium** (pip ignores `download_url`; the MIT notice is preserved verbatim so there is no violation; nothing is published). Listed as High only to keep it visible in the pre-publish gate — which is exactly what R5/R34 are. |
| H26 | High | The sdist omits `requirements.txt`, which `setup.py` reads at build time | `setup.py:6-10,20` (`:20` is the `parse_requirements(...)` call — the README embedding is a separate site at `:17-18,28`, see MG8); no `MANIFEST.in` | **R4**, R1 | Breaks `pip install --no-binary :all:`, air-gapped mirrors, distro packagers. Invisible locally because pip prefers the wheel — R1's sdist-install job is the check. R4 removes the build-time file read entirely. |
| H27 | High | No exception hierarchy; the one exception class is vestigial and unchained | `utils/exceptions.py:6`; raised only at `http_file_config.py:62`, caught nowhere | **R10** | No single base to `except` on, no retryable/terminal distinction, `raise ... from e` appears zero times, and `super().__init__()` is never called so `Exception.args` is empty and instances cannot survive pickling. |
| H28 | High | Client certificates have never worked — the mTLS path fails **closed** | `helpers/internal/filters_helper.py:27` | **R23** | Reclassified from C5 after execution proved CPython refuses to build a client socket from a `PROTOCOL_TLS_SERVER` context. Broken functionality, not a MITM hole. FI-2: the fix makes the path connect for the first time ever. |
| H29 | High | `request()` has three mutually incompatible response envelopes, and the obvious success check can never fail | `logic/ftp.py:66`, `logic/sftp.py:60` (`return True`) vs `logic/http.py:70`; `async_gateway.py:105` | **R8** | Added by the adversarial pass — a seam defect no single lane could see. `if result['api_response']:` is truthy in all three shapes. Breaking; bundled with the R5 version reset. FI-7. |

### Medium (28)

| Finding ID | Sev | One-line summary | `file:line` | Requirement(s) | Notes |
|---|---|---|---|---|---|
| M1 | Medium | `return {'ssl': verify_ssl or True}` is always `True` — the documented `verify_ssl` flag is inoperative | `helpers/internal/filters_helper.py:33` | **R23** | Fails *secure*, hence Medium — but the library's TLS posture is currently an accident of a truthiness bug, and the obvious cleanup refactor turns it into a live MITM switch. R23's criterion is an explicit decision, not a cleanup. |
| M2 | Medium | `verify_ssl=True` without a certificate assigns `None` and silently downgrades FTPS to plaintext | `logic/ftp.py:43-44` | **R15** | `get_ssl_config` returns the `ssl_context` key only when a certificate is supplied. FI-1: must be fixed with C6, or fixing C6 exposes a silent downgrade instead of a crash. |
| M3 | Medium | `client.stat()` runs unconditionally, so a successful deletion is reported as a `999` failure | `logic/ftp.py:62-64` | **R15** | Retries then re-attempt a completed delete. |
| M4 | Medium | File metadata derived by string-parsing an undocumented asyncssh repr | `logic/sftp.py:34-37` | **R17** | `IndexError` on any fragment without `':'`, `KeyError` if no `type` key — both landing as bogus `999`s **before** the transfer is attempted. Use the typed `SFTPAttrs` fields. |
| M5 | Medium | `request_type == 'GET'` compared case-sensitively while every other consumer lowercases | `helpers/internal/filters_helper.py:61` | **R12** | `"get"` attaches the payload as a JSON body on a GET. |
| M6 | Medium | `str(True)` → `"True"`, `type(x) in [bool]` instead of `isinstance`, unhandled `None`/`list`/`datetime`, and mutates the caller's payload in place | `helpers/internal/filters_helper.py:65` | **R12** | APIs expect `"true"`. The in-place mutation joins M12 and M28 as the third instance of the same root cause. |
| M7 | Medium | Multipart writes `str(bytes)` reprs to a text-mode file, truncates parts over 8192 bytes, and raises on a `None` part | `helpers/internal/request_helper.py:74` | **R13**, R20 | Exactly one `read_chunk()` per part. `reader.next()` returning `None` → `AttributeError`. Also one of C3's blocking sites. |
| M8 | Medium | A malformed JSON response is indistinguishable from a legitimate empty object | `helpers/internal/response_helper.py:15` | **R13** | `except ValueError: text = {}` — the caller gets `json: {}` with no error flag. |
| M9 | Medium | `UnicodeDecodeError` sets `error_message` but never `text`, which is then read unconditionally → `KeyError` | `helpers/internal/request_helper.py:131`; read at `logic/http.py:60` | **R13** | The specific diagnostic is discarded and replaced by a generic `999`. |
| M10 | Medium | Download-config keys read with `.get()` and no default, though the README documents them as optional | `helpers/internal/request_helper.py:121,124` | **R12** | Following the docs passes `None` to `iter_chunked()` and raises. |
| M11 | Medium | `on_connection_reuseconn` overwrites the request-start baseline mid-request | `utils/request_tracer.py:50` | **R26** | Every subsequent metric under-reports latency on keep-alive connections, and it stores an absolute timestamp among 13 relative deltas. Wrong observability data misdirects triage. |
| M12 | Medium | Mutates the caller's `circuit_breaker_config` dict in place | `helpers/internal/base.py:45` | **R24** | Pollutes a reused `protocol_info` with a live `RetryPolicy` after the first call. |
| M13 | Medium | Retry defaults are a thundering herd — delay 0, max_delay 0, jitter False | `utils/constants.py:10-11` | **R24** | Immediate, un-spaced, synchronised retries against an already-failing dependency, with no working breaker (H8) to stop amplification. |
| M14 | Medium | Exponential backoff selected by the magic string `name == 'backoff'` while the README documents `name` as "Any name" | `helpers/internal/circuit_breaker_helper.py:52`; `README.md:170` | **R24** | A caller following the docs silently gets a constant 0-second delay and their `max_delay`/`jitter` are ignored. |
| M15 | Medium | Circuit-breaker config is an untyped `**kwargs` bag read with `.get()`, so any typo is silently discarded | `helpers/internal/circuit_breaker_helper.py:16` | **R24** | `max_failures`, `reset_timeout` → default used, no error. |
| M16 | Medium | No breaker isolation by destination | `helpers/internal/circuit_breaker_helper.py` (no host/service key) | **R24** | FI-4: the obvious fix for H8 (a module global) would trade a dead breaker for one flaky host opening the circuit for every destination. |
| M17 | Medium | No path containment anywhere — a hostile server can emit `../` on a recursive download | `grep abspath\|realpath\|normpath\|commonpath\|Path(` → **zero** across `async_gateway/` | **R22** | `download_filepath`, `local_filepath`, `remote_path`, `server_path`/`client_path` all reach `open()` or the client verbatim. A Zip-Slip-shaped primitive, made materially more likely by C4. |
| M18 | Medium | Writes to a caller-supplied path with no `O_EXCL`/`O_NOFOLLOW` and no mode restriction | `utils/http_file_config.py:68` | **R22** | The README's own examples use fixed `/tmp/test.pdf` and `/tmp/temp.png` — a symlink pre-creation attack on a shared host. |
| M19 | Medium | The documented cleanup step raises `FileNotFoundError` if the file is already gone; no `try/finally` orphans partial downloads | `utils/http_file_config.py:72` | **R22** | Cleanup must be idempotent; a partially-written file is orphaned on every failure. |
| M20 | Medium | No redaction discipline on the returned envelope; the tracer stores a live exception carrying the caller's `Authorization` | envelope aggregation across `logic/*`; `utils/request_tracer.py:103`; `README.md:394` | **R8**, R26, R10 | The single response dict aggregates raw `url`, `payload`, all headers and `dict(resp.cookies)` including `Set-Cookie`, and the README demonstrates printing it. Not secrets-in-logs (there is no logger yet) but an amplifier: the obvious "log the response" transitively logs credentials — and R10 adds the logger. R8's redactor covers **all four** named surfaces (headers/cookies, `auth`, the URL's userinfo and sensitive query parameters, and key-name masking of the payload echo to depth 4); invariant E9 is written to claim exactly that and no more, and the README states plainly that a payload below depth 4 is echoed verbatim. |
| M21 | Medium | SSRF: the library neither documents that the caller owns URL validation nor offers an opt-in guardrail | README (`grep -i "ssrf\|validate\|sanitiz\|untrusted\|allowlist"` → zero hits) | **R21** *(partial — see Deferral summary)*, R14 | "The caller chooses the URL" is the library's purpose, not a defect. R21 delivers the documentation, `allowed_schemes`, **and per-redirect-hop enforcement of it** (Ruling 4, via R14's owned redirect loop); only the host-allowlist and validator-hook guardrails are deferred, with justification. |
| M22 | Medium | No `pyproject.toml` / PEP 621 / PEP 517 declaration; no `py.typed`, so every annotation is invisible downstream | repo root; `async_gateway/` | **R4** | All metadata is executable code, which is also the mechanism behind H26. FI-13: `py.typed` only after mypy is clean, or you export your type errors to your users. |
| M23 | Medium | No `python_requires` despite the 3.10 classifier | `setup.py` | **R4** | No resolver reads a classifier. Medium only because the transitive pins accidentally block old interpreters, so the user gets a baffling cascade rather than a clean `Requires-Python`. See **OQ3**. |
| M24 | Medium | Sphinx cannot build: zero `.rst` files, Makefile in the wrong directory, `make.bat` Windows-only | `docs/` (exactly 3 files) | **R31** | `SOURCEDIR = source` inside `docs/source/` resolves to `docs/source/source`. Two dev dependencies exist for a build that has never succeeded. |
| M25 | Medium | `getattr()` on caller-supplied verb strings at five sites, with no allowlist | `logic/sftp.py:43`, `logic/ftp.py:51`, `request_helper.py:31,100`, `http_file_config.py:57` | **R21** | Was H13; down-rated by the adversarial pass, restoring a 3-1 lane majority. No privilege boundary is crossed — `mode` *is* the documented verb — the defect is an undocumented, unbounded capability surface with no allowlist. `request_type='close'` yields `TypeError`, swallowed as `999`. |
| M26 | Medium | `pytest-asyncio` is not declared, so no async test can be written at all | `requirements-dev.txt` (zero matches for `pytest-asyncio`, `anyio`, `pytest-aiohttp`, `aioresponses`) | **R2** | Every meaningful unit in this library is `async def`. Combined with C8 a contributor hits a trap: a normally-named test is silently deselected; one named to match `pre_test` errors for an unrelated reason. **This blocked the audit's own testing step.** |
| M27 | Medium | `tests/coverage_output.py:13` breaks the moment branch coverage is enabled | `tests/coverage_output.py:13` | **R2** | `int("0.5432")` → `ValueError`. Line 10 does it correctly for `line-rate`. Not a test (pytest never collects it) and currently dead (no `--cov-report=xml`). FI-5: deleted before R28 enables `--cov-branch`. |
| M28 | Medium | A third caller-dict mutation, at the highest-blast-radius site | `logic/sftp.py:41` | **R17** | `sftp.py:23` → `base.py:31` → `async_gateway.py:104` all pass the caller's dict by reference. After one directory `get`, the caller's `protocol_info['additional_arguments']` is permanently `{'recurse': True}` and is inherited by every later call sharing that config — including a later single-file `remove`. Under `asyncio.gather` with one shared config (the library's stated use case) that is a cross-request state leak. |

### Medium, grouped — ids assigned by this spec (8)

> These eight are real Medium findings that live in an **unnumbered prose block** at
> `repo-audit-report.md:638-648`, structurally identical to the Low block. They are given the `MG`
> series rather than `M29`–`M36` so the verified ground truth "Medium is exactly M1–M28" stays
> greppable. Flagged for ratification as **OQ1**.

| Finding ID | Sev | One-line summary | `file:line` | Requirement(s) | Notes |
|---|---|---|---|---|---|
| MG1 | Medium | `pyfailsafe==0.6.0` is dormant (sole release 2021-02-06, no `requires_python`) and sits on the critical resilience path | `requirements.txt:6` | **R7** | Already the latest release, so there is nothing to upgrade to. R7 defines the decision procedure and the evidence that decides it; neither a silent keep nor a silent swap is acceptable. |
| MG2 | Medium | `orjson` and `requests` are declared and imported by zero files | `requirements.txt:2,3` | **R3** | Each carries an advisory that is unreachable precisely because nothing imports them. R3 completes the `orjson` migration and drops `requests`. |
| MG3 | Medium | `TIMEZONE = 'Asia/Kolkata'` hardcoded into a published package, bound at import time, with no override | `utils/constants.py:4`; `helpers/common/date_helper.py:9` | **R9** | R9 emits UTC ISO-8601 with an explicit offset; no `TIMEZONE` constant, no import-time binding, no display timezone (cut — see "Deliberately cut sub-features"). |
| MG4 | Medium | `CHUNK_SIZE_CONSTANT = 1024` is ~64× smaller than conventional | `utils/constants.py:12` | **R14** | ~100,000 await cycles on a 100 MB upload. |
| MG5 | Medium | `fetch_file` is dead code; removing it also retires the whole `helpers/common/file_helper.py` duplicate | `helpers/internal/request_helper.py:13`; `helpers/common/file_helper.py` | **R25** | The duplicate is H15's second copy with a different parameter order. |
| MG6 | Medium | The README documents **no SFTP section at all** while the protocol is fully implemented and registered | `README.md` (zero `sftp` matches in 1,126 lines) | **R29**, R16 | A user of the current package has no way to learn their SFTP credentials are unprotected (C4). |
| MG7 | Medium | The root `CLAUDE.md` "Commands" block documents `uvicorn`/`ruff`/`app/` for a library with none of them | `CLAUDE.md` §Commands | **R32** | Actively misleading to future contributors and to every agent session. **Requires explicit user approval to edit** (project-wide file). |
| MG8 | Medium | The README ships a live corporate endpoint (`api.fyndx1.de`) **19 times**, republished inside every sdist | `README.md` ×19; `setup.py:17-18` (reads it), `:28` (embeds it as `long_description`) | **R29** | Verified: `grep -c` returns 19. Citation corrected: `setup.py:20` is `parse_requirements(...)`, which is H26's defect, not this one. |

### Low / Cosmetic (17) — ids assigned by this spec

| Finding ID | Sev | One-line summary | `file:line` | Requirement(s) | Notes |
|---|---|---|---|---|---|
| L1 | Low | `List[aiohttp.TraceConfig()]` subscripts a generic with an **instance** | `logic/http.py:26` | **R30** | Harmless only because CPython does not evaluate that annotation in function scope. Becomes a real error under R30's `disallow_incomplete_defs`. |
| L2 | Low | Annotated subscript targets — legal, but neither evaluated nor stored; pure noise | `async_gateway.py:97,105,108` | **R30** | Deleted. `:105` is also where H29's `Dict` annotation sits over an assigned `True`. |
| L3 | Low | Latency measured with `time.time()`, so an NTP step yields negative latency | `logic/http.py:64` | **R9** | On the exact metric users dashboard. R9 moves to `time.monotonic()`. |
| L4 | Low | `start_time: int` annotation on a float | `helpers/internal/base.py:32` | **R9**, R30 | |
| L5 | Low | Class docstring says "ftp request class" in the SFTP module | `logic/sftp.py:13` | **R30**, R17 | |
| L6 | Low | `self.remote_files` is dead and mistyped — and looks like the initialisation H2 needs | `logic/sftp.py:22` | **R17** | Deleted in the same commit as the H2 fix precisely so it cannot mislead the fixer. |
| L7 | Low | Docstring documents a "Sqlalchemy session object" in a library with no database | `helpers/internal/request_helper.py:88` | **R30** | |
| L8 | Low | Structurally dead `if ...: pass` branch | `helpers/internal/filters_helper.py:62` | **R12** | The branch that silently drops a file upload on a GET; R12 decides that case explicitly instead of removing the branch alone. |
| L9 | Low | Truthiness fallback makes `maximum_failures=0` unreachable | `helpers/internal/circuit_breaker_helper.py:18` | **R24** | |
| L10 | Low | `isinstance(x, int)` silently rejects floats while accepting `bool` | `helpers/internal/circuit_breaker_helper.py:54` (identical sibling at `:49`) | **R24** | Two line references, one defect class; the `:49` sibling is in the same fix's scope. |
| L11 | Low | Uses `pytz` where the stdlib suffices (import `:6`, import-time binding `:9`); and `get_ist_now` is named for IST while accepting any timezone (`:12`) | `helpers/common/date_helper.py:6,9,12` | **R9** | One id: all of it is resolved by rewriting that one function (grouping rule 3). R9 removes `pytz` outright and needs no `zoneinfo`/`tzdata` replacement, because the display-timezone sub-feature is cut. |
| L12 | Low | Implicit-Optional and legacy `typing.Text` | `utils/exceptions.py:12` | **R10**, R30 | The file is rewritten by R10 regardless. |
| L13 | Low | `STATUS_CODE_403 = 403` — a constant named after its own value | `utils/constants.py:6` | **R25** | Its only consumer is the URL-download status check that R25 replaces. |
| L14 | Low | Module docstrings absent or content-free across the tree | `utils/__init__.py` and `helpers/__init__.py` (0 bytes); `"""Constants."""`; `"""Ftp."""` | **R30** | One id, tree-wide (grouping rule 1). R30's criterion is content, not presence. |
| L15 | Low | Import grouping violates stdlib / third-party / first-party ordering | `base.py`, `filters_helper.py`, `circuit_breaker_helper.py`, `ftp.py`, `sftp.py` | **R27**, R30 | One id across five files (grouping rule 1). Cannot currently be detected at all: `application_import_names = async-gateway` is an illegal identifier (H19). |
| L16 | Low | `open('README.md')` without `encoding=` | `setup.py:17` | **R4** | Latent only — the README is currently pure ASCII (verified). R4 deletes `setup.py`. |
| L17 | Low | Claims `Development Status :: 5 - Production/Stable` for a package that does not import | `setup.py:37` | **R4** | R4 requires the classifier be chosen deliberately and justified in the CHANGELOG. |

> **Not an `L` id, by design:** the audit's trailing **type-annotation-coverage measurement**
> (47 functions, 45 public, 28 lacking a return annotation, 22 with an unannotated parameter; worst
> offenders the 15 tracer callbacks, the three `handle_request()` overrides, and 5 of 7 functions in
> `request_helper.py`) is a measurement, not a located defect. It is folded into **R30** (full
> annotations, enforced by `disallow_untyped_defs` / `disallow_incomplete_defs`) and **R27** (mypy at
> zero errors with `ignore_errors` removed), whose criteria are count-independent.

---

## Dependencies

### Runtime (target state)

| Package | Target | Change | Why it exists |
|---|---|---|---|
| `aiohttp` | `>=3.14.3,<4` | Upgraded from `~=3.9.5` (a range admitting exactly one version, forever) | HTTP + SOAP transport |
| `orjson` | `>=3.12.0,<4` | Loosened; **now actually imported** (R3) | JSON serialisation |
| `aioboto3` | `>=15.5.0,<16` | Upgraded from `==13.1.1`; forces R25 | S3 file download |
| `aiofiles` | `>=25.1.0,<26` | Upgraded from `~=24.1.0`; **now used on all four blocking sites** (R20) | Async file I/O |
| `aioftp` | `>=0.28.0,<1` | Upgraded from `==0.22.3` | FTP / FTPS |
| `asyncssh` | `>=2.24.0,<3` | Upgraded from `==2.15.0` | SFTP |
| `pyfailsafe` | `0.6.0` — **or replaced, or vendored** | **R7 decides this on evidence.** Already the latest release; the project is dormant | Retry + circuit breaker |
| `pytz` | **removed at Step 5** | Carried at `>=2026.3.post1,<2027` through Phase 0 because `date_helper` still imports it; R9 removes import and declaration together. Then UTC only; stdlib `datetime.timezone.utc` suffices, so no `zoneinfo`/`tzdata` replacement is needed either (Ruling 3, OQ11). Fallback: keep the pin **only** if removal proves infeasible, with the reason written down | — |
| `requests` | **removed at Step 4** | Declared, imported by zero files (MG2); R3 drops it | — |
| *(XML)* | **none** — stdlib `xml.etree.ElementTree` | R19's decision, justified there | SOAP parsing |

All versions above the "target" column are the human-pre-approved set (locked decision 3). **No
dependency is added, removed, or upgraded beyond this table without user approval.**

> **What "live-verified 2026-08-15" did and did not establish.** It was a **resolver** check: the seven
> constraints co-resolve, and re-executing it confirms a clean
> `Would install … aiohttp-3.14.3 … orjson-3.12.0 … pyfailsafe-0.6.0` set on Python 3.12. A resolver
> check proves the versions *exist and are mutually satisfiable*. It proves **nothing** about whether
> a package works at runtime against them — which is exactly the gap that let A5 pass two reviews.
> Every runtime claim in this spec that depends on a version is closed by execution or is marked as
> open; see the standing constraint under **Assumptions**.

### Development

Add: `pytest-asyncio` (with `asyncio_mode = "auto"`), **`pytest-randomly`** (required
by R28's order-independence criterion and R1's shuffled-order job — absent from `requirements-dev.txt`
today, which is why that criterion was previously unexecutable), `build`, `twine`, a formatter, and
either `setuptools<81` or the removal of `flake8-import-order` (R2/H19).

**Deliberately *not* added: any aiohttp-mocking library.** Revision 2 listed `aioresponses`. It is
removed (Ruling A): `aioresponses 0.7.9` is the terminal release and is broken against the entire
aiohttp 3.14.x line — it resolves cleanly against `aiohttp>=3.14.3,<4` and then raises
`TypeError: ClientResponse.__init__() missing 1 required keyword-only argument: 'stream_writer'` on
every mocked request (A5, executed). The HTTP/SOAP fixture is a **loopback `aiohttp.web` test server
via `aiohttp.test_utils.TestServer`**, which needs no dependency beyond `aiohttp` itself and cannot lag
it by construction. Pinning `aioresponses` alongside an older aiohttp "for some tests" is explicitly
rejected — a second aiohttp in the dev set is the same trap one layer down.

Keep: `flake8` + its plugins,
`mypy`, `pytest`, `pytest-cov`, `lxml` (test fixtures only), and **`bandit` — which now has a CI step
that runs it** (R1); a declared scanner nothing invokes is the same dead-control pattern as C8/H18/H19,
so if the maintainer would rather not run it, it is removed instead of left declared. Remove if R31's
recommended path is taken: `sphinx`, `sphinx-rtd-theme`.

### Requirement ordering — the hard dependencies between requirements

Package dependencies are above; these are the **requirement-to-requirement** dependencies that a story
planner must respect. Each is load-bearing: if the depended-on requirement is descoped or weakened, the
dependent requirement's argument silently fails.

| Requirement | Hard-depends on | Why — what breaks if the dependency is weakened |
|---|---|---|
| **R19** (SOAP Faults + XML hardening) | **R14** (`max_response_bytes`) | R19's justification for stdlib `xml.etree` is that the prolog `DOCTYPE` guard closes the entity-expansion class **and the byte cap bounds everything else** (unbounded tree size, decompression bombs). If the cap is descoped or defaulted high, OQ6's ratification no longer holds and the XML decision must be re-made. R19 carries its own capped-read criterion so the dependency is testable, not merely asserted. |
| **R18** (SOAP client) | **R14** (session, timeout, capped read), **R12** (raw-body filter), **R8** (envelope), **R24** (breaker), **R26** (tracer) | R18's entire design is "a fourth strategy over the existing transport". Every one of those is a surface it reuses rather than reimplements; R12's raw-body branch in particular is what stops a SOAP envelope being routed to the JSON filter. |
| **R21** (`allowed_schemes`) | **R14** (owned redirect loop) | The scheme guardrail is only true per hop, and only the owned loop can refuse a hop. See FI-16. |
| **R14**, **R23**, **R25**, **R26** (the aiohttp- and aioboto3-touching surfaces) | **R6** landing first, at Step 1.5 | **The direction of this dependency is inverted from Revision 2.** Each of these four is *written against* the upgraded set rather than migrated onto it afterwards, so R6 must land before any of them — and before any test exists at all. If R6 slips after them, the whole HTTP/SOAP test architecture is built on a transport the release replaces, which is FI-10 and the plan critique's Critical. |
| **R6** (dependency upgrade) | **R2** landing first (Step 1 builds the harness) | R6's own gate is "the upgraded set installs, resolves and imports, and one request round-trips through the fixture" — which needs the fixture and a runnable async test to exist. That is the **only** reason R6 is Step 1.5 rather than Step 1. It lands on `requirements.txt`, which still exists in Phase 0; **R4 (Step 2) then transcribes the already-upgraded set into `pyproject.toml`**, so the target versions are written down once. R6 does **not** depend on R23/R25/R26 any more — see FI-10. |
| **R9** (UTC timestamps) → **R6**'s `pytz` removal | R9 lands first, at Step 5 | `date_helper` imports `pytz` at module scope, so the declaration cannot be dropped at Step 1.5 without turning Step 3's clean-venv import red. Same shape for R3 (Step 4) and `requests`. |
| **R27** (mypy clean) → **R4** (`py.typed`) | R4's marker added only after R27 is green | FI-13: publishing the marker early exports this library's type errors downstream. |
| **R30** (docstrings) → **R27** (docstring lint rules) | R30 lands first | FI-8: otherwise the baseline suppression file is created for a class of finding about to be fixed wholesale. |
| **R28** (coverage) | **R24**'s clock/sleep seam | R28 forbids sleep-based waiting; without an injectable clock its timing criteria have nothing to drive. |
| **R2** (delete `coverage_output.py`) → **R28** (`--cov-branch`) | R2 first | FI-5. Now tighter than before: R28 enables `--cov-branch` at **Step 1**, in the same step as the deletion. |

### Process and tooling dependencies

- **GitHub Actions** availability for R1. If the project is not on GitHub Actions, the same job graph
  must exist on whatever CI the repository uses; the requirement is the job graph, not the vendor.
- **Human approval** is a hard dependency for three things: editing `CLAUDE.md` and
  `.claude/rules/fastapi-patterns.md` (R32), adopting any dependency not in the table above (R7
  path 4a), and the LICENSE copyright decision (R34).
- **The audit report** (`.claude/state/repo-audit-report.md`) is the input backlog and must remain in
  the repository for the traceability table's `file:line` citations to be checkable.

---

## Out of scope

Explicitly **not** built in `1.0.0`. Anything not traceable to a finding id or to one of the eight
locked human decisions is out of scope by default; these are the ones worth naming because someone
will otherwise assume them.

- **Publishing to PyPI.** The release must be *ready* to publish — `sdist` + `wheel` build, `twine
  check` passes, metadata is correct — but the upload itself stays a manual human action outside this
  pipeline (locked decision 4).
- **WSDL support of any kind** — no WSDL fetching, no introspection, no code generation, no
  dict-to-XML mapping derived from a schema (locked decision 1, a fixed boundary).
- **SOAP MTOM / attachments (`multipart/related`)** — explicitly rejected with a clear error (R18),
  not silently mis-parsed.
- **A richer SSRF guardrail** — host allowlists and caller-supplied validator hooks. See the Deferral
  summary. `allowed_schemes`, its **per-redirect-hop enforcement**, and the documentation are all in
  scope (Ruling 4).
- **A caller-supplied display timezone** (`protocol_info['timezone']`, `request_time_local`). Cut —
  see "Deliberately cut sub-features". `request_time` is UTC ISO-8601 with an explicit offset and the
  caller converts it in one line if they want a local rendering.
- **An opt-out from redaction** (`redact=False`). Cut — its only function is to put credential values
  back into the envelope and, through R10's logger, into the caller's logs.
- **New protocols.** The packaging description currently advertises XML and redis, neither of which
  exists anywhere. R18 removes the advertisement; it does not implement them.
- **Async-generator/streaming response APIs for consumers.** The library returns a materialised
  envelope; a streaming consumer API is a feature, not a defect fix.
- **Rewriting the pre/post-processor design.** It is not the subject of any finding. Its behaviour is
  documented (R29) and its error path is brought into the envelope (R8), and that is all.
- **A synchronous API.** This is an async library.
- **Performance optimisation beyond the named findings** (MG4's chunk size, H11's pooling, H12's
  bounded reads, M7's quadratic accumulator). No profiling-driven work is scoped.
- **Backward-compatibility shims for the `2.7.3` surface.** There are no consumers; per the project's
  "delete the path you superseded" rule, no aliases are kept for `api_response`, `tat`, or the old
  `logic/*` module names.
- **Deployment, containerisation, and runtime observability infrastructure.** This is a library with no
  deployable surface; the DevOps gate covers CI only, and the Observability gate covers the library's
  own logging discipline (R10) only.

---

## Assumptions

Each of these is a belief this spec acts on that has not been proven from the read-only position it was
written in. Any of them being false changes the plan.

| # | Assumption | If it is false |
|---|---|---|
| A1 | **The package is still unpublished at implementation time.** Every "breaking change is free" argument in this spec rests on it. | R5's version reset becomes impossible (you cannot go down from a published version) and R8's envelope change becomes a genuine breaking change requiring a major-version story and a migration note. Re-verify `pypi.org/pypi/async-gateway/json` immediately before starting. |
| A2 | **The seven pre-approved dependency versions still exist and still resolve together** on the target Python. They were live-verified 2026-08-15. | R6's story re-verifies and, if a version has been yanked, escalates rather than silently picking a neighbour. |
| A3 | **The audit's `file:line` citations still match `31542aa`.** This spec re-read every source file and confirmed the load-bearing ones; a few line numbers drift by 1-2 (e.g. H14's `request_helper.py:216` is the `content_type` assignment; the failing call is at `:217-218`). | Citations are navigational, not contractual; the acceptance criteria name behaviour, not lines. |
| A4 | **There is no consumer of `async_gateway.logic.http` (or any other internal module path).** | R27's rename to `*_client.py` would need a deprecation shim. Verified as far as possible: the package is unpublished and the README only documents `async_gateway.async_gateway.request`. |
| A5 | ~~**`aioresponses` (or an equivalent) can mock the aiohttp version R6 selects.**~~ **No longer an assumption — executed, and FALSE.** See the decided-fact box below the table. | *(Was: "R2's tooling story escalates; the fallback is a local `aiohttp.web` test server.")* The fallback is now the plan: Ruling A makes the `aiohttp.web` test server the primary HTTP/SOAP fixture and removes `aioresponses` from R2, Step 1 and the dev dependency set. |
| A6 | **`asyncssh` and `aioftp` can be driven entirely through mocks to 100% branch coverage** without a live server. | R28's coverage target would need a containerised server fixture in CI — a materially larger testing story. **With A5 resolved, this is again the single biggest *open* schedule risk in the plan** (the plan critique correctly noted that while A5 stood, A5 was). Note that A5's resolution does not help here: the `aiohttp.web` fixture is an aiohttp-specific answer and gives `asyncssh`/`aioftp` nothing. |
| A7 | **The repository's git history is authoritative for provenance.** The initial commit contains only `.gitignore` + `LICENSE`, with the whole codebase squashed in afterwards, so the 2.x lineage is *not* in this repository. | R34's copyright decision needs information from outside the repository — which is why it is a human decision (OQ7). |
| A8 | **`pyfailsafe` will import and function on the R6 dependency set.** **Half-executed.** The *import* half holds: `failsafe` imports cleanly on Python 3.12.14 and 3.14.7 alongside the full target set. The *function* half is what R7's behaviours 1–6 measure at Step 22. Behaviour 7 (clock/sleep injectability) is **executed and failed** — see R7. | R7's path 4 is the answer if behaviours 1–6 fail. Behaviour 7's failure is already resolved by Ruling D (keep-with-named-cost, R24 owns the seam) rather than left open. |
| A9 | **The maintainer wants the enterprise-profile gates enforced on an ongoing basis**, not just satisfied once for the release. | R33's changelog-CI step and R28's `fail_under=100` are the parts most likely to be resented later; both are called out with cut criteria. |

### Decided fact (was A5): `aioresponses` is unusable on the aiohttp this release targets

Not an assumption. **Executed**, by the plan critique and re-executed independently by the
orchestrator in a fresh venv. `aioresponses 0.7.9` is the **terminal** release
(`pip index versions aioresponses --pre` → `LATEST: 0.7.9`; no newer, no pre-release). It declares
`aiohttp<4.0,>=3.8`, so it **resolves cleanly** against `aiohttp>=3.14.3,<4` — and then fails at
runtime on every mocked request:

```
aiohttp 3.14.3   FAIL  TypeError: ClientResponse.__init__() missing 1 required keyword-only argument: 'stream_writer'
aiohttp 3.14.0   FAIL  (same)
aiohttp 3.13.2   OK    status=200 body=hi
aiohttp 3.12.15  OK
aiohttp 3.11.18  OK
aiohttp 3.9.5    OK    (the version pinned today)
```

The replacement was verified on the same aiohttp 3.14.3:
`aiohttp.test_utils.TestServer` + `aiohttp.web` round-trips a POST and captures the request body and
Content-Type — `RESULT: (200, 'pong')`, `CAPTURED: {'body': '<Envelope/>', 'ctype': 'text/xml'}` — so
the byte-identity and per-hop header assertions R18, R14 and R21 need are achievable on it.

**Why this was invisible to two reviews.** Both EM passes verified dependency compatibility at the
**metadata/resolver** level and treated a clean resolve as compatibility. Neither had an execution
tool. A resolver check cannot see this class of break, and a read-only reviewer cannot close an
execution-shaped assumption.

### Standing constraint on this plan: version-support assumptions are closed by execution only

> **An assumption of the form "tool X supports version Y" is closed only by executing one call through
> X against Y.** It is never closed by reading X's `requires_dist`, by a `pip install --dry-run`, by a
> changelog, or by the absence of a known issue.

This binds every remaining assumption of that shape in this document, and it is enforced, not merely
stated: **R2/Step 1 carries an acceptance criterion that drives one request through the chosen HTTP
fixture against the *target* aiohttp**, so the class of failure that produced this section surfaces on
day one rather than at step 21 of 31. Runtime-patching test doubles (`aioresponses`, `responses`,
`freezegun`, `moto`) are the highest-risk instances: they declare permissive ranges and break
*silently inside them*.

---

## Open questions

These require a human decision **before** implementation starts, or before the specific requirement
they gate can be considered final. Each names what is blocked and what this spec recommends.

**OQ1 — Ratify the `MG1`–`MG8` series.**
The audit's Medium section closes with an unnumbered prose block (`repo-audit-report.md:638-648`)
containing eight further Medium findings — structurally identical to the Low block, but not flagged in
the report's headers or in this task's verified inventory. This spec assigned them the `MG` series
rather than `M29`–`M36` so that the ground truth "Medium is exactly M1–M28" stays mechanically
greppable, and mapped all eight to requirements. **Decision needed:** accept the `MG` series as-is,
renumber them into the `M` range (and update the ground-truth count to 36), or rule any of them out of
scope. *Recommendation: accept as-is.* **Blocks:** nothing — the work is scoped either way; only the
id scheme is in question.

**OQ2 — The mypy error count contradicts itself (40 vs 44).**
`repo-audit-report.md:39` says `ignore_errors = True` hides "44 real errors"; the H18 finding body at
`:426-430` reports the measured run as `Found 40 errors in 9 files`. This spec cannot resolve it from a
read-only position and deliberately does not let it matter: R27's criterion is `mypy async_gateway`
exiting 0 with zero errors and no `ignore_errors` anywhere. **Decision needed:** none, unless someone
intends to use the count as a progress metric — in which case measure it fresh rather than trusting
either number. **Blocks:** nothing.

**OQ3 — Confirm the `python_requires` floor.**
This spec proposes `>=3.10`, matching the existing (cosmetic) classifier. The real floor is whatever
the R6 dependency set supports, and that cannot be verified from here. **Decision needed:** confirm
`>=3.10`, or set it from the resolved dependency set at implementation time. *Recommendation: resolve
mechanically during R6 and let a CI matrix over the declared range be the proof.* **The matrix is no
longer only a recommendation** — R1 now carries it as an acceptance criterion (a job per released minor
interpreter in the declared range), because it is also the evidence R7's dormancy decision needs and
the proof the NFR table's portability row claims.

**A second decision now sits inside this one — the `requires-python` *ceiling*.** The plan critique
found the matrix stale at exactly the point the spec says the ceiling is load-bearing: it listed
`3.10`–`3.13` while CPython **3.14 is released** (`3.14.7`) and in range for an unbounded `>=3.10`.
3.14 has been added. But the deeper defect was the claim that "the list is generated from
`requires-python` … so the matrix cannot drift" — **no test can derive a finite matrix from an
unbounded range**, so that mechanism never existed. R1 now specifies the honest one: a committed list,
a test that it is ≥ the floor and contiguous, and a scheduled job that fails when a newer stable minor
exists. **Decision needed:** ratify that, or instead **bound `requires-python`** (e.g. `>=3.10,<3.15`)
so the range genuinely is finite and derivable. *Recommendation: ratify the maintained list. A ceiling
on a library blocks every consumer on the day a new interpreter ships, to buy a property only the
matrix generator wanted.* **Blocks:** R4's metadata; the concrete interpreter list in R1's matrix.

**OQ4 — Confirm the version reset is still safe (one-way door).**
`2.7.3` → `1.0.0` is safe **only** while PyPI 404s (A1). Once anything is uploaded under this name, the
reset becomes impossible and the version must go forward instead. **Decision needed:** confirm nothing
has been uploaded, and confirm nobody intends to upload before this lands. *Recommendation: re-verify
the 404 immediately before R5's story starts and record the check in the ticket work log.* **Blocks:**
R5, and by extension R8's "breaking change bundled with the version reset" argument.

**OQ5 — SOAP 1.2 interop: spec-conformant only, or also emit `SOAPAction`?**
SOAP 1.2 carries the action as a `Content-Type` parameter and defines no `SOAPAction` header; some
real-world servers nonetheless require the header. This spec chose **spec-conformant only**, on the
grounds that an interop escape hatch nobody has asked for is speculative configurability. **Decision
needed:** ratify, or add an opt-in `soap_action_header: bool`. *Recommendation: ratify; add the flag
when a real endpoint demands it.* **Blocks:** R18's header criterion.

**OQ6 — Ratify the XML parsing decision.**
Locked decision 1 required this be decided and justified; R19 chose **stdlib
`xml.etree.ElementTree` plus a mandatory pre-parse `DOCTYPE` rejection**, with `lxml` staying dev-only
and `defusedxml` rejected as itself dormant since 2021. The reasoning is written out in R19. **Decision
needed:** ratify, or direct that `lxml` be promoted to a runtime dependency (which would need the
`library-review` evaluation and user approval per locked decision 3's spirit). *Recommendation:
ratify.* **Blocks:** R19; the runtime dependency table.

**OQ7 — The LICENSE copyright line.**
`LICENSE:3` reads `Copyright (c) 2022 Fynd`; the packaging metadata reads `author='Arjunsingh Yadav'`;
the 2.x lineage is not in this repository's history (A7). The MIT notice is preserved verbatim, so
there is no violation — this is attribution, and it is not a decision an agent can make. **Decision
needed:** retain the upstream copyright line, extend it to name both parties, or change it.
*Recommendation: retain the upstream line and add the fork's own copyright as a second line — the
conservative reading of MIT's "the above copyright notice … shall be included".* **Blocks:** R34; the
first publish.

**OQ8 — Approval to edit `CLAUDE.md` and `.claude/rules/fastapi-patterns.md`.**
Both describe a FastAPI service (`uvicorn app.main:app`, `ruff`, an `app/` package, a router → service
→ repository recipe) that this library does not have, and both are on the project-wide-files list
requiring explicit approval. **Decision needed:** approve the edit, or decline and accept that every
future agent session starts from a wrong command block. *Recommendation: approve.* **Blocks:** R32. If
declined, MG7 stays open in the traceability table with status "rejected by human" — it is not
silently dropped.

**OQ9 — Is `logic/*.py` → `logic/*_client.py` acceptable?**
R27 resolves the `A005` stdlib-shadowing finding (`logic/http.py` shadows stdlib `http`) by renaming
three protocol modules (plus `soap_client.py`, which is created under its final name and is not a
rename), rather than by suppressing the rule. It is free today (no consumers) and leaves no suppression
to justify. **Decision needed:** ratify the rename, or accept a per-module `# noqa: A005` with a
written justification. *Recommendation: ratify the rename.* **Blocks: Step 4.5 — which is in Phase 0**,
so this must be answered **before implementation starts**, not before Phase 6. The rename moved early
(FI-15) because every test path, traceability row and later step in this spec is written in post-rename
names; that also moved this question's deadline forward. Also blocks R27's zero-suppression criterion
and the file structure in Part B.

**OQ10 — Does the maintainer accept `fail_under = 100` and the changelog CI gate as ongoing policy?**
Locked decision 7 fixes 100% line + branch coverage for this release. Whether it stays enforced
afterwards is a maintenance-appetite question, and an enforced gate the maintainer resents is the one
that gets disabled at the first inconvenient moment — exactly the failure mode that produced C8, H18
and H19. **Decision needed:** confirm the gates stay on after `1.0.0`, or agree a documented
step-down (e.g. `fail_under = 95` post-release) now rather than by silent erosion later.
*Recommendation: keep 100 through `1.0.0` and revisit explicitly at `1.1.0`.* **Blocks:** nothing for
this release; it is the durability question the whole plan rests on. *(The path to 100 is now a
ratchet rather than a single flip — R28 — which is what makes the question about maintenance appetite
rather than about schedule.)*

**OQ11 — Ratify a strictly-reducing deviation from the pre-approved dependency list: `pytz` removed,
and no `zoneinfo`/`tzdata` replacement either.**
Orchestrator Ruling 3 directed that `pytz` be removed in favour of stdlib `zoneinfo`, with `tzdata`
declared as a Windows-conditional dependency. Applying the EM's scope cut to R9 (no caller-supplied
display timezone, no `request_time_local`) removes the last IANA lookup from the library, so
`datetime.timezone.utc` is sufficient and **neither `zoneinfo` nor `tzdata` is needed on any
platform**. The net effect is one dependency removed and none added — strictly less than the ruling
asked for, in the ruling's own direction. It is flagged rather than applied silently because the
pre-approved dependency list is a locked human decision and any deviation from it, including a
reducing one, is the human's to accept. **Decision needed:** ratify the removal, or direct that
`zoneinfo` + a Windows-conditional `tzdata` be carried anyway against a future display-timezone
feature. *Recommendation: ratify — carrying a platform-conditional dependency for a feature this
release deliberately cut is exactly the speculative surface §2a.5 rung 6 forbids.* **Blocks:** R9's
implementation and the runtime dependency table. *(If instead the display timezone is reinstated, R9's
`tzdata` edge case and its platform branch come back with it, along with their branch-coverage cost.)*

**OQ12 — Ratify keeping `pyfailsafe` with a named cost, now that behaviour 7 is known-failed.**
Revision 2 left the keep/replace/vendor choice to Step 22, contingent on a fitness behaviour that has
since been **executed and failed**: `pyfailsafe 0.6.0` hardcodes `time.monotonic()`
(`circuit_breaker.py:139,142`) and `await asyncio.sleep(...)` (`failsafe.py:103`) and exposes no clock
or sleep parameter. Leaving a decision whose outcome is already determined four steps upstream of
Step 26's terminal coverage gate — with the *replace* and *vendor* branches unestimated — put the one
non-negotiable gate behind an open question. Orchestrator **Ruling D** therefore decides it in this
spec: **keep `pyfailsafe`, with the named cost recorded, and make R24's own breaker facade hold the
`clock`/`sleep` seam** rather than expecting the dependency to grow one. The facade lands at Step 14
and is independent of Step 22's outcome, so the coverage gate is no longer hostage to it.
**The named cost is larger than Ruling D first stated, and the corrected figure is what this question
turns on.** The two places `pyfailsafe` reads the clock and sleeps are the open → half-open timing and
the retry backoff wait — but neither is separable from the code around it. Counting and opening are one
operation (`circuit_breaker.py:124-125` increments then trips; `open()` immediately stamps
`time.monotonic()` at `:139`), so a delegated count engages the library's clock and defeats the
injected one. The three callbacks and the backoff sleep are *configured* on `RetryPolicy` but *invoked*
by `Failsafe.run`, the sole entry point (`failsafe.py:59,92,98,103,105,107`), so owning the wait means
owning the loop. **The facade therefore owns the entire breaker state machine and the entire retry
loop**, and what the dependency still supplies is **exception classification and backoff computation
only** — `retry_policy.py`, **136 of `pyfailsafe`'s 539 lines, roughly 40 of them non-trivial**.

**Decision needed:** ratify keep-with-facade, or direct *replace* or *vendor* now (each of which needs
its own scoped steps, which this plan does not currently carry). *The comparison, stated honestly so
the answer is not foregone:* the facade — and its `clock`/`sleep` seam, its state machine and its retry
loop — must be written under **all three** branches, and its interface is identical under all three, so
none of that work is a differentiator. What the three branches actually differ on is only what sits
beneath it. **Keep** buys a ~136-line (~40 non-trivial) residual at the price of a fifth dormant
dependency on the critical resilience path (MG1). **Vendor** absorbs that residual into a module R7
already bounds at **≤ 250 lines** at 100% branch coverage — a bound set against the *full* breaker-plus-
retry surface, so most of it is the facade being written anyway; against a ~40-non-trivial-line residual
this is a genuinely close call, not a fallback. **Replace** trades dormancy for a new dependency and a
`library-review` pass, and is the only branch that adds an approval step. *No recommendation is offered:
the residual is now small enough that reasonable answers differ, and "no silent keep, no silent swap"
(locked decision 5) makes this the human's call rather than the plan's.* **Blocks:** nothing in Phase 0;
R7's ADR and Step 22's evidence record the answer either way, and Step 14's facade proceeds regardless.

---
---

# Developer Documentation: async-gateway v1.0.0

## Architecture overview

`async-gateway` has one public function and a protocol-strategy layer behind it. The target
architecture keeps that shape — it is the right shape — and fixes the seams.

```
                      ┌─────────────────────────────────────────────┐
   caller ──────────▶ │ async_gateway.async_gateway.request()       │  ← the ONLY public entry point
                      │  · normalise + validate protocol  (R11)     │
                      │  · run pre_processor                        │
                      │  · build the envelope skeleton    (R8, R9)  │
                      │  · dispatch                                 │
                      │  · run post_processor                       │
                      │  · return GatewayResponse         (R8)      │
                      └───────────────────┬─────────────────────────┘
                                          │  protocol_mapping: dict[str, type[BaseRequestClass]]
                        ┌─────────────────┼─────────────────┬───────────────────┐
                        ▼                 ▼                 ▼                   ▼
                 HttpRequest        FtpRequest        SftpRequest         SoapRequest
              (http_client.py)   (ftp_client.py)   (sftp_client.py)    (soap_client.py)
                        │                 │                 │                   │
                        │                 │                 │      envelope-construct + parse (R18/R19)
                        └────────┬────────┴─────────────────┴───────────────────┘
                                 │        all four inherit
                        ┌────────▼──────────────────────────────────────────────┐
                        │ BaseRequestClass  (helpers/internal/base.py)           │
                        │  · timeout · certificate · verb allowlist   (R21)      │
                        │  · circuit breaker LOOKUP (not construction) (R24)     │
                        │  · abstract handle_request() -> GatewayResponse        │
                        └────────┬──────────────────────────────────────────────┘
                                 │
   ┌─────────────────────────────┼──────────────────────────────────────────────────┐
   ▼                             ▼                          ▼                        ▼
 transport helpers          cross-cutting utils        error model              observability
 request_helper.py          envelope.py     (R8)       exceptions.py  (R10)     request_tracer.py (R26)
 filters_helper.py          paths.py        (R22)      status_map.py  (R8)      module loggers    (R10)
 response_helper.py         redaction.py    (R8)                                 
                            date_helper.py  (R9)
                            breaker_registry (R24)
```

**What changes structurally, and why.**

1. **SOAP slots in as a fourth strategy, not a fifth architecture.** `SoapRequest` subclasses
   `BaseRequestClass` like the others and delegates its transport to the *same* `request_helper`
   machinery `HttpRequest` uses — same session and pooling (R14), same timeout enforcement (R14), same
   breaker (R24), same tracer (R26), same envelope (R8). SOAP-specific logic is exactly three things:
   envelope construction, version-correct headers, and response/Fault parsing. Nothing else in the
   library needs to know SOAP exists beyond one registry entry.

2. **The envelope moves from "whatever each protocol felt like returning" to one builder.** Today the
   entry point creates a dict, passes it by reference into the protocol object, HTTP silently replaces
   it with a *different* fresh dict on success and mutates the original on failure, and FTP/SFTP ignore
   it and return `True`. In the target, `utils/envelope.py` owns a single `GatewayResponse` builder; the
   entry point seeds it, each protocol *fills* it, and nobody constructs a response shape locally.
   This is FI-7: the defect is at the seam, so the fix has to be too.

3. **Errors stop being a return value and become exceptions until the boundary.** Protocol code raises
   typed `AsyncGatewayError` subclasses; **one** place — the entry point — converts an exception into
   an envelope. That is what lets programming errors (`KeyError`, `TypeError`, `UnboundLocalError`)
   propagate out of `request()` instead of masquerading as a `999`.

4. **Per-process state gains exactly one home.** The circuit-breaker registry is the only genuinely
   process-lifetime state the library holds; it lives in one module with a bounded LRU keyed by
   `(protocol_family, host, port)`. Everything else stays per-request.

5. **Nothing is added that no requirement asks for.** No plugin system, no configuration framework, no
   abstract transport interface with one implementation. The three new modules (`envelope.py`,
   `paths.py`, `redaction.py`) each exist because two or more call sites need the same behaviour and
   getting it wrong in one of them is a finding in this backlog.

**Data flow for one HTTP call, end to end:**

```
request(url, data, auth, protocol='HTTPS', protocol_info={...})
 ├─ normalise protocol → 'HTTPS'; validate against registry              R11
 ├─ parse URL scheme; 'HTTPS' + http:// → ConfigurationError             R11
 ├─ envelope = new_envelope(url=url, protocol=protocol, payload=data)    R8 R9
 ├─ pre_processor(response=envelope, **params)      → envelope['pre_processor_response']
 ├─ HttpRequest(url, auth, envelope, info).handle_request()
 │   ├─ breaker = registry.get(('http', host, port))                     R24
 │   ├─ session = info['session'] or ClientSession(timeout=..., ...)     R14
 │   ├─ filters = filter_for_content_type(headers, payload, verb)        R12
 │   ├─ ssl_filters = get_ssl_config(certificate, verify_ssl)            R23
 │   ├─ breaker.run(make_http_request, ...)  ← retry body via factory    R14 R24
 │   │    ├─ status/headers/cookies → envelope (redacted)                R8
 │   │    ├─ bounded read + decode                                       R14 R13
 │   │    └─ parse body by media type                                    R12 R13
 │   ├─ envelope['request_tracer'] = per-request trace results           R26
 │   └─ envelope['latency'] = monotonic() - start                        R9
 ├─ post_processor(response=envelope, **params)     → envelope['post_processor_response']
 └─ return envelope
       ▲
       └─ on AsyncGatewayError: envelope['ok']=False, ['error']=..., ['status_code']=map(exc)   R8 R10
          on any other exception: PROPAGATE                                                      R10
```

---

## File structure — current vs target

```
REPO ROOT
  pyproject.toml                              NEW      R4   PEP 621 + 517; flake8/mypy/pytest/coverage config
  .flake8                                     NEW      R4   (only if flake8 config cannot live in pyproject)
  MANIFEST.in                                 NEW      R4   sdist file set (H26)
  setup.py                                    DELETED  R4   executable metadata; build-time file read (H26, L16, L17)
  setup.cfg                                   DELETED  R4   `-k pre_test` (C8), `ignore_errors` (H18), bad import names (H19)
  requirements.txt                            DELETED  R4   folded into [project.dependencies]
  requirements-dev.txt                        DELETED  R4   folded into [project.optional-dependencies.dev]
  README.md                                   REWRITTEN R29  consumer-facing (H24, MG6, MG8)
  CHANGELOG.md                                NEW      R33
  LICENSE                                     VERIFIED R34  copyright decision is human (OQ7)
  CLAUDE.md                                   MODIFIED R32  Commands block (MG7) — NEEDS USER APPROVAL
  .github/workflows/ci.yml                    NEW      R1   build → clean-venv wheel+sdist import → lint → mypy → tests → twine
  examples/http_example.py                    NEW      R35
  examples/ftp_example.py                     NEW      R35
  examples/sftp_example.py                    NEW      R35
  examples/soap_example.py                    NEW      R35
  examples/error_handling_example.py          NEW      R35
  docs/make.bat                               DELETED  R31  Windows-only, no working build path
  docs/source/Makefile                        DELETED  R31  wrong directory (resolves to docs/source/source)
  docs/source/conf.py                         DELETED  R31  release='2.1' (H22); no .rst root doc (M24)
  docs/specs/v1_release_spec.md               NEW      —    this file
  docs/decisions/NNNN-resilience-library.md   NEW      R7   ADR recording the pyfailsafe decision

async_gateway/
  __init__.py                                 MODIFIED R5 R10  0 bytes → __version__ + logging NullHandler
  py.typed                                    NEW      R4   ADDED LAST, only after mypy is clean (FI-13)
  async_gateway.py                            MODIFIED R11 R8 R10  dispatch, validation, envelope seeding, exception→envelope boundary

  logic/__init__.py                           MODIFIED R11 R18  typed registry; 'SOAP' → real class (H3)
  logic/http.py       → logic/http_client.py  RENAMED at STEP 4.5 (R27/A005), then MODIFIED R8 R12 R13 R14 R23
  logic/ftp.py        → logic/ftp_client.py   RENAMED at STEP 4.5 (R27/A005), then MODIFIED R15 (C6, H1, M2, M3)
  logic/sftp.py       → logic/sftp_client.py  RENAMED at STEP 4.5 (R27/A005), then MODIFIED R16 R17 (C4, H2, M4, M28, L5, L6)
  logic/soap_client.py                        NEW (logic/soap.py, 0 bytes, is DELETED at Step 4.5)  R18 R19 (H3)

  helpers/__init__.py                         MODIFIED R30  0 bytes → module docstring (L14)
  helpers/common/__init__.py                  MODIFIED R30
  helpers/common/date_helper.py               MODIFIED R9   stdlib datetime UTC ISO-8601, no pytz/zoneinfo, rename (MG3, L11)
  helpers/common/file_helper.py               DELETED  R25  verbatim duplicate with a different arg order (H15, MG5)

  helpers/internal/__init__.py                MODIFIED R12  shared media-type matcher; no exact-key mapping (H14)
  helpers/internal/base.py                    MODIFIED R11 R21 R24  info guard (H5), verb allowlist, breaker LOOKUP not construction (H8), no caller-dict mutation (M12), start_time: float (L4)
  helpers/internal/circuit_breaker_helper.py  REWRITTEN AS A FACADE  R7 R24  owns clock/sleep and therefore the whole breaker state machine + retry loop (Ruling D — pyfailsafe has no seam; only classification + backoff computation delegated); typed config (M15), reachable 0 (L9), isinstance (L10), named backoff (M14)
  helpers/internal/breaker_registry.py        NEW      R24  bounded LRU keyed by (family, host, port) (M16)
  helpers/internal/filters_helper.py          MODIFIED R12 R23  orjson (C1), media-type matching (H14), GET case (M5), bool coercion + no mutation (M6), dead branch (L8), SERVER_AUTH (H28), no `or True` (M1)
  helpers/internal/request_helper.py          MODIFIED R8 R13 R14 R20 R25  typed HttpResult, no kwargs.get('response') (FI-7/H29), aiofiles (C3), retry body factory (H9), timeouts (H10), pooling (H11), bounded reads (H12), owned redirect loop + per-hop scheme check (R21/EM-H2), multipart (M7), decode (M9), defaults (M10), dead fetch_file (MG5), docstring (L7)
  helpers/internal/response_helper.py         MODIFIED R13  orjson (C1), malformed vs empty (M8)

  utils/__init__.py                           MODIFIED R30  0 bytes → module docstring (L14)
  utils/constants.py                          MODIFIED R9 R14 R24  no TIMEZONE (MG3), chunk size (MG4), retry defaults (M13), no STATUS_CODE_403 (L13)
  utils/envelope.py                           NEW      R8   GatewayResponse TypedDict + the single builder (H7, H29)
  utils/status_map.py                         NEW      R8 R10  exception/protocol → status_code, one table
  utils/exceptions.py                         REWRITTEN R10  hierarchy, chaining, super().__init__ (H27, L12)
  utils/redaction.py                          NEW      R8 R10  one redactor used by the envelope AND the logger (M20)
  utils/paths.py                              NEW      R22  resolve_within + safe-write helpers (M17, M18, M19)
  utils/http_file_config.py                   MODIFIED R20 R22 R25  aiofiles.os.remove (C3), aioboto3 API (H15), all statuses (H16), safe writes (M18), idempotent delete (M19)
  utils/request_tracer.py                     MODIFIED R26  per-request context (H17), relative reuseconn (M11), string not exception (M20), annotations (R30)

tests/
  __init__.py                                 KEPT
  test_pass.py                                DELETED  R2   the tautology `assert 'True' == 'True'` (C8)
  coverage_output.py                          DELETED  R2   dead, and breaks on --cov-branch (M27)
  conftest.py                                 NEW      R2 R28  the loopback aiohttp.web recording test server (HTTP/SOAP — NOT a mocking lib), mock aioftp / asyncssh / aioboto3, tmp paths, controlled clock
  test_entrypoint.py                          NEW      R11 R8   dispatch + envelope invariants across 5 protocols × {ok, fail}
  test_envelope.py                            NEW      R8 R9    key set, serialisability, no self-alias, redaction, latency
  test_exceptions.py                          NEW      R10      hierarchy, chaining, unwrap_cause, never-empty message, propagation
  logic/test_http_client.py                   NEW      R12 R13 R14
  logic/test_ftp_client.py                    NEW      R15
  logic/test_sftp_client.py                   NEW      R16 R17
  logic/test_soap_client.py                   NEW      R18 R19
  helpers/test_filters_helper.py              NEW      R12 R23
  helpers/test_request_helper.py              NEW      R13 R14 R20
  helpers/test_circuit_breaker.py             NEW      R7 R24   the seven fitness behaviours + registry + clock seam
  utils/test_paths.py                         NEW      R22      traversal table
  utils/test_http_file_config.py              NEW      R25
  utils/test_request_tracer.py                NEW      R26
  test_no_blocking_io.py                      NEW      R20      AST assertion over the whole package
  test_docs.py                                NEW      R29 R30  README compile/import/signature/payload-echo-disclosure tests; module-docstring content test
  test_packaging.py                           NEW      R4 R5    version single-source, wheel/sdist contents, py.typed present
```

**Module boundary rules (unchanged from the existing shape, made explicit):**

- `async_gateway.py` is the only module that knows about pre/post processors, and the only module that
  converts an exception into an envelope.
- `logic/*_client.py` know about their protocol and nothing about each other.
- `helpers/internal/*` are transport mechanics; they raise, they never build a response shape.
- `utils/*` are leaf utilities with no imports from `logic/` or `helpers/` (this keeps the import graph
  acyclic and is what makes `envelope.py`/`exceptions.py` safely importable from everywhere).

---

## Data models and the public API contract

### `GatewayResponse` — the single return type of `request()`

This is the resolution of **H29** and the library's one public contract. Defined once in
`async_gateway/utils/envelope.py`.

```python
class GatewayError(TypedDict):
    type: str            # exception class name, e.g. "TimeoutError"
    code: str            # STABLE machine-readable code — see the code table below
    message: str         # human-readable; NEVER empty when ok is False
    cause: str | None    # the unwrapped __cause__ chain's deepest type+message, or None

class GatewayResponse(TypedDict):
    ok: bool                          # THE success predicate — the only correct check
    status_code: int                  # always populated; see the mapping table
    protocol: str                     # 'HTTP' | 'HTTPS' | 'SOAP' | 'FTP' | 'SFTP'
    url: str                          # redacted: no userinfo, sensitive query params masked
    request_time: str                 # ISO-8601, UTC, explicit offset — UTC only, no local variant
    latency: float                    # seconds, from time.monotonic()
    payload: Any                      # echo of the request payload, key-name-redacted to depth 4
    text: str                         # decoded body; '' when not applicable; NEVER an exception
    json: dict | list | None          # parsed body, or None
    headers: dict[str, str]           # {} for FTP/SFTP; credential headers redacted
    cookies: dict[str, str]           # {} for FTP/SFTP; redacted
    error: GatewayError | None        # None iff ok is True
    protocol_details: dict[str, Any]  # per-protocol extras — see below
    request_tracer: list[dict]        # per-request trace results; [] when tracing is off
    pre_processor_response: Any | None
    post_processor_response: Any | None
```

**Invariants, each of which is a test:**

| # | Invariant | Enforces |
|---|---|---|
| E1 | The key set is **identical** for all five protocols on both the success and the failure path. | H29 |
| E2 | `ok is False` ⟺ `error is not None`. | H29, C2 |
| E3 | `error['message']` is never `''` when `ok is False`. | C2 (`str(RetriesExhausted()) == ''`) |
| E4 | No value in the envelope is the envelope itself, at any depth. | H7 (`r['api_response'] is r`) |
| E5 | `json.dumps(envelope)` succeeds on **every** path. | H7, C2 (today: `TypeError` on the failure path) |
| E6 | `text` is always a `str`. | C2 (today: a live exception object) |
| E7 | `status_code` is never `999`, and `999` appears nowhere in the package. | C2 |
| E8 | `latency` is `>= 0` even across a wall-clock step. | L3 |
| E9 | No credential value carried in a **header** (`Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`), a **cookie**, an **`auth` object**, or the **URL** (netloc userinfo, or the value of a query parameter whose name is in the sensitive-name set) appears anywhere in `repr(envelope)`. The `payload` echo is masked by **key name** against the same set to a **depth of 4**; below that depth, and for non-mapping payloads, the caller's own data is echoed **verbatim** and the README says so. | M20 |
| E10 | `api_response` and `tat` are absent. | H29 |
| E11 | On a **remote-status failure** (HTTP 4xx/5xx, a SOAP Fault at any status, an FTP/SFTP status error) `status_code`, `headers`, `cookies`, `text` and `json` are populated exactly as on the success path. `ok=False` never costs the caller the response body. | H29, and the exception-based control flow that would otherwise drop it |

> **On E9's wording.** An earlier draft asserted that *no* credential value appears anywhere in
> `repr(envelope)`, while `utils/redaction.py` was specified only for header names and
> `aiohttp.BasicAuth`. A URL carrying `?api_key=…` or a payload carrying `{'password': …}` satisfied
> that redactor and violated that invariant — an invariant the design could not keep. Both halves have
> been fixed: the redactor now covers the URL and the payload's key names (R8), and E9 now claims
> exactly what the redactor delivers and no more. An invariant that overclaims is worse than a narrow
> one, because a consumer reads it as a guarantee.

> **On E11.** The architecture converts protocol errors into envelopes at exactly one point
> (`finalise_error`). Nothing about that design *by itself* requires the protocol to have written the
> response body into the envelope before raising — so without E11 a `404` carrying a JSON error body
> would return `text=''`, `json=None`. That is the single most common way a consumer uses a failure
> response, it is a regression against today's behaviour, and it is precisely the kind of gap an
> exception-based refactor introduces silently. Hence an invariant and a named test, not a convention.

**`protocol_details` by protocol** (the only per-protocol variation, and it is a nested dict so the
top-level key set stays invariant):

| Protocol | Keys |
|---|---|
| HTTP / HTTPS | `{}` (everything is top-level) |
| SOAP | `soap_version`, `soap_body` (`Element \| None` — **the first element child of `<Body>`**, not `<Body>` itself; `None` when `<Body>` is absent or empty; the first child with a logged `warning` when `<Body>` has several), `soap_fault` (`SoapFault \| None`) |
| FTP | `command`, `server_path`, `client_path`, `file_stats` |
| SFTP | `mode`, `remote_path`, `local_path`, `file_stats` (typed `SFTPAttrs` fields), `files` (`list[str] \| None`) |

**Deliberately removed** (no alias, no shim — there are no consumers, and a shim would preserve the
trap): `api_response` (the self-alias vector and the truthiness trap — its absence makes old code fail
loudly with `KeyError`), `tat` (renamed `latency`), `external_call_request_time` (renamed
`request_time` and moved to UTC), `error_message` (folded into the structured `error`).

**Deliberately never added:** `request_time_local` (the display-timezone sub-feature is cut — see
"Deliberately cut sub-features"), and any `redact` switch.

### `status_code` mapping — one table, in `utils/status_map.py`

There is no protocol-neutral status concept, so the library defines one explicitly rather than
inventing a `999`.

| Situation | `status_code` | `error.code` | `ok` |
|---|---|---|---|
| HTTP / HTTPS / SOAP success | the real HTTP status | — | `True` |
| HTTP / HTTPS 4xx/5xx | the real HTTP status | `HTTP_STATUS` | `False` |
| SOAP Fault (any transport status, **including 200**) | the real HTTP status | `SOAP_FAULT` | `False` |
| FTP success | the server's FTP reply code (2xx), else `200` | — | `True` |
| FTP failure | the server's FTP reply code (4xx/5xx), else `500` | `FTP_STATUS` | `False` |
| SFTP success | `200` | — | `True` |
| SFTP `SSH_FX_NO_SUCH_FILE` | `404` | `SFTP_STATUS` | `False` |
| SFTP `SSH_FX_PERMISSION_DENIED` | `403` | `SFTP_STATUS` | `False` |
| SFTP other `SFTPError` | `500` | `SFTP_STATUS` | `False` |
| SSH host-key verification failure | `495` *(library-assigned; documented)* | `HOST_KEY` | `False` |
| Timeout (connect, read, or total) | `504` | `TIMEOUT` | `False` |
| Circuit open | `503` | `CIRCUIT_OPEN` | `False` |
| DNS / connect / TLS failure | `502` | `DNS` / `CONNECT` / `TLS` | `False` |
| Response over the size cap | `502` | `RESPONSE_TOO_LARGE` | `False` |
| Body could not be parsed / serialised | `502` (response side) / `400` (request side) | `SERIALIZATION` | `False` |
| XML rejected before parse (DOCTYPE) | `502` | `XML_UNSAFE` | `False` |
| Path containment violation | `400` | `PATH` | `False` |
| Caller configuration error | `400` | `CONFIG` | `False` |

`error.code` values are **wire-stable**: the human-readable `message` may change freely between
releases; the code may not. This is what lets a consumer branch on `error['code']` rather than on
message text.

### Exception hierarchy — `utils/exceptions.py`

```
AsyncGatewayError(Exception)                    ← the ONE base a consumer catches
├── ConfigurationError                          → 400  CONFIG      caller's fault, not retryable
│   └── UnsupportedVerbError                    → 400  CONFIG      (R21 allowlist)
├── SerializationError                          → 400/502  SERIALIZATION
│   └── UnsafeXmlError                          → 502  XML_UNSAFE  (R19 DOCTYPE guard)
├── PathContainmentError                        → 400  PATH        (R22)
├── TransportError                              → 502
│   ├── ConnectError                            → 502  CONNECT
│   ├── DnsError                                → 502  DNS
│   ├── TlsError                                → 502  TLS
│   ├── HostKeyError                            → 495  HOST_KEY    (R16)
│   ├── GatewayTimeoutError                     → 504  TIMEOUT
│   └── ResponseTooLargeError                   → 502  RESPONSE_TOO_LARGE
├── CircuitOpenError                            → 503  CIRCUIT_OPEN
└── ProtocolError                               → per protocol
    ├── HttpStatusError                         → real status  HTTP_STATUS
    ├── FtpStatusError                          → reply code   FTP_STATUS
    ├── SftpStatusError                         → mapped       SFTP_STATUS
    └── SoapFaultError                          → real status  SOAP_FAULT
```

Every class:
- calls `super().__init__(message)` so `Exception.args` is populated and instances survive pickling
  across `ProcessPoolExecutor`/Celery (the current `CustomGlobalException` does neither);
- carries `.code` (the wire-stable string above) as a class attribute;
- is raised with `raise ... from exc`, enforced by lint rule `B904`.

`CustomGlobalException` is **removed**, not deprecated — it is raised in exactly one place and caught
nowhere.

### `SoapFault`

```python
class SoapFault(TypedDict):
    version: str                 # '1.1' | '1.2'
    code: str                    # 1.1 faultcode | 1.2 Code/Value
    subcodes: list[str]          # 1.2 Code/Subcode chain; [] for 1.1
    reason: str                  # 1.1 faultstring | 1.2 Reason/Text
    actor: str | None            # 1.1 faultactor | 1.2 Role
    detail: str | None           # raw XML of <detail>/<Detail>, or None
```

### `CircuitBreakerConfig` — typed, not a `**kwargs` bag

```python
class RetryConfig(TypedDict, total=False):
    name: str
    allowed_retries: int          # required
    retriable_exceptions: list[type[BaseException]] | None
    abortable_exceptions: list[type[BaseException]] | None
    on_retries_exhausted: Callable[..., Any] | None
    on_failed_attempt: Callable[..., Any] | None
    on_abort: Callable[..., Any] | None
    delay: float                  # default: non-zero (M13)
    max_delay: float              # default: non-zero (M13)
    jitter: bool                  # default: True (M13)
    backoff: Literal['constant', 'exponential']   # replaces the magic name=='backoff' (M14)

class CircuitBreakerConfig(TypedDict, total=False):
    maximum_failures: int         # 0 is a legal, reachable value (L9)
    timeout: float
    retry_config: RetryConfig

    # --- the test seam (R24, R28) -------------------------------------------
    clock: Callable[[], float]                    # default: time.monotonic
    sleep: Callable[[float], Awaitable[None]]     # default: asyncio.sleep
```

Unknown keys raise `ConfigurationError` naming the key (M15). Numeric fields accept `int` and `float`
and reject `bool` (L10). The caller's dict is copied before any `retry_policy` is written into it
(M12).

**`clock` and `sleep` are the library's only timing seam, and they are part of the interface rather
than a test detail.** Every duration the resilience layer measures — the breaker's `reset_timeout_seconds`
countdown, half-open eligibility, retry backoff spacing — reads `clock()`; every wait it performs calls
`await sleep(d)`. Both default to the real implementations, so a consumer never sees them; tests
substitute a `FakeClock` (an advanceable counter) and a recording `sleep` (which appends the requested
duration and returns immediately). Two criteria depend on this existing:

- **R24** — "half-open after `reset_timeout_seconds`, tested with a controlled clock" and "three
  retries are spaced, two concurrent sequences are not synchronised";
- **R28** — "no `sleep`-based waiting anywhere".

Neither is satisfiable if the timing lives inside a dependency that owns its own clock — and the
dependency this library uses **does** own its own clock. That is settled, not speculative:
`pyfailsafe 0.6.0` hardcodes `time.monotonic()` at `circuit_breaker.py:139,142` and
`await asyncio.sleep(...)` at `failsafe.py:103`, with no parameter for either (R7, fitness behaviour 7,
executed and failed).

**So the seam is implemented here, in this library, and not asked for from the dependency**
(orchestrator Ruling D). `helpers/internal/circuit_breaker_helper.py` is a **facade**:

| Owned by the facade (reads `clock()`, awaits `sleep(d)`) | Delegated to the resilience implementation |
|---|---|
| the breaker state machine — closed / open / half-open and every transition | retriable / abortable exception classification (`retry_policy.py`: `should_abort`, `_is_retriable_exception`) |
| failure counting against `maximum_failures`; `record_success` / `record_failure` | the retry backoff **computation** — constant / exponential / jitter / `max_delay` (`Backoff.for_attempt`) |
| open → half-open eligibility timing | — |
| the retry loop — breaker consult, abort branch, the backoff **wait**, and the `CircuitOpen` / `RetriesExhausted` raises | — |
| the three callback **invocations** (`on_retries_exhausted`, `on_failed_attempt`, `on_abort`) | — |

**The split cannot be drawn any narrower, and the source is why.** Counting and opening are one
operation: `record_failure` does `self.current_failures += 1` and trips the breaker on the next line
(`circuit_breaker.py:124-125`), and `open()` immediately stamps `self.opened_at = time.monotonic()`
(`circuit_breaker.py:139`) — so a delegated count engages the library's own clock and defeats the
injected one. A breaker configured never to open reduces "delegated counting" to `self.n += 1`. The
callbacks and the wait are likewise inseparable from the loop: they are *configured* on `RetryPolicy`
but *invoked* by `Failsafe.run`, the library's sole entry point (`failsafe.py:59`), at
`failsafe.py:92,98,105,107` around the `await asyncio.sleep(wait_for)` at `failsafe.py:103`. Owning the
wait means owning that loop; owning the half-open clock means owning the state machine.

What remains delegable is `retry_policy.py` alone — **136 of `pyfailsafe`'s 539 lines, of which roughly
40 are non-trivial**. It is a severe cost — it narrows what `pyfailsafe` is still worth to two pure
functions — and it is recorded as such in R7's ADR with an owner and a revisit trigger, not absorbed
silently; **OQ12** puts the resulting keep / replace / vendor comparison to the human. It is also
what makes the facade's interface **identical under keep, replace and vendor**, so R24 (Step 14) does
not wait on R7 (Step 22) and R28's terminal gate does not sit behind an open dependency decision.

The seam is threaded through the registry (`get_breaker`, below) so the protocol classes never
construct it themselves — they look it up, per H8.

Documented default for `reset_timeout_seconds` and `maximum_failures` stays as R24 specifies; the seam
adds no behaviour, only observability of time.

---

## Component interfaces

Every entry below states inputs, outputs, and **what errors it can produce and how they surface**.

### `async_gateway.async_gateway.request` — the public entry point

```
async def request(
    url: str,
    data: dict | str | bytes | None = None,
    auth: aiohttp.BasicAuth | None = None,
    protocol: str = '',
    protocol_info: dict | None = None,
    pre_processor_config: ProcessorConfig | None = None,
    post_processor_config: ProcessorConfig | None = None,
    **kwargs: Any,
) -> GatewayResponse
```

- **Inputs.** `protocol` is normalised (strip + upper) once and validated against the registry.
  `protocol_info` is protocol-specific and validated at this boundary only — internal layers trust it.
- **Output.** Always a `GatewayResponse` satisfying E1–E11. Never `True`, never a bare dict, never a
  self-referential structure.
- **Errors.**
  - Raises `nothing` for any *remote* or *transport* failure — those become `ok=False` envelopes.
  - Raises `ConfigurationError` for caller mistakes that make the call unformable (unknown protocol,
    `protocol=None`, missing required `protocol_info` key, HTTPS+`http://` URL, unknown verb, unknown
    circuit-breaker key). **Rationale:** these are programming errors on the caller's side and are not
    retryable; returning an envelope for them would encourage a retry loop against a call that can
    never succeed. This is a deliberate asymmetry and it is documented in the README.
  - **Propagates** `KeyError`, `TypeError`, `AttributeError`, `asyncio.CancelledError` and any other
    non-`AsyncGatewayError` exception unchanged. This is the property whose absence hid C6, H6 and most
    of the audit.
- **Side effects.** Runs the caller's pre/post processors. A processor that raises produces
  `ok=False` with `error.code == 'CONFIG'` and the processor's exception as `cause`; it does not
  prevent the envelope from being returned.

### `BaseRequestClass` — the protocol strategy contract

```
class BaseRequestClass(abc.ABC):
    def __init__(self, url: str, auth: Any, response: GatewayResponse, info: dict) -> None
    @abc.abstractmethod
    async def handle_request(self) -> GatewayResponse
```

- **Inputs.** `response` is the envelope skeleton created by the entry point; the subclass **fills**
  it and returns it. `info` is already validated and already a **copy** — no protocol class ever
  mutates a caller-owned dict (M6, M12, M28).
- **Output.** The same envelope object it was given, populated. Never a new dict, never `True`.
- **Errors.** Raises `AsyncGatewayError` subclasses only; the entry point converts them.
- **Provides to subclasses:** `self.timeout` (float, always set), `self.certificate`,
  `self.breaker` (looked up from the registry, **not constructed** — H8), and
  `self.resolve_verb(name, allowlist)` (R21).

### `helpers/internal/request_helper.py` — the transport boundary (FI-7's HTTP half)

```python
class HttpResult(TypedDict):
    status_code: int
    headers: dict[str, str]
    cookies: dict[str, str]
    text: str
    body: bytes | None            # None once a streamed download has been written to disk
    redirect_chain: list[str]     # every hop actually followed, in order (R21's per-hop check)

async def make_http_request(
    session: aiohttp.ClientSession,
    url: str,
    filters: dict,
    request_type: str,
    *,
    timeout: aiohttp.ClientTimeout,
    max_response_bytes: int,
    allowed_schemes: frozenset[str],
    max_redirects: int,
    allow_redirects: bool,
    ...
) -> HttpResult
```

- **There is no `json` field on `HttpResult`, deliberately.** The transport boundary returns bytes and
  text, not parsed content: `json` is produced by `response_helper`'s media-type-driven parse of
  `text`/`body` inside `logic/http_client.py` (R12/R13), and it is `http_client` — not
  `make_http_request` — that puts `json` on the envelope. So wherever the envelope's `json` is named
  alongside `HttpResult`'s fields (invariant E11's sequence, `finalise_error`'s preserved key set), it
  is the envelope's key being named, not a field copied straight out of this TypedDict.
- **This signature is the fix for `request_helper.py:93`.** Today the function does
  `response = kwargs.get('response', {})` — a **fresh** dict that `logic/http.py` never passes in — and
  then returns it as if it were the caller's envelope, silently dropping the `url`, `payload`,
  `external_call_request_time` and `error_message` that `request()` had already put there. That is why
  "fix H29 by making FTP and SFTP `return self.response`" produces three envelopes that are *still*
  different: HTTP's was never the same object in the first place.
- **The boundary rule, made mechanical:** `helpers/internal/*` return **data** (`HttpResult`), never a
  response shape; `logic/*_client.py` copy that data into the envelope they were handed; only
  `utils/envelope.py` ever constructs a `GatewayResponse`. R8's criterion
  `grep -rn "kwargs.get('response'" async_gateway/` → zero is the check, and **Step 5** performs the
  deletion.
- **Errors.** Raises `AsyncGatewayError` subclasses only — `ResponseTooLargeError` on the cap,
  `GatewayTimeoutError`, `ConnectError`/`DnsError`/`TlsError`, `ConfigurationError` for a redirect
  target outside `allowed_schemes`. **Before raising a remote-status error the caller (`http_client`)
  has already copied this result into the envelope**, which is invariant E11.

### `utils/envelope.py`

```
def new_envelope(*, url: str, protocol: str, payload: Any) -> GatewayResponse
def finalise_ok(env: GatewayResponse, *, status_code: int, started: float) -> GatewayResponse
def finalise_error(env: GatewayResponse, exc: AsyncGatewayError, *, started: float) -> GatewayResponse
```

- **Inputs.** `new_envelope` redacts as it builds: `url` through `redact_url`, `payload` through
  `redact_payload`. There is no `timezone` parameter — `request_time` is
  `datetime.now(timezone.utc).isoformat()`, full stop.
- **Errors.** None of the three raises. `new_envelope` has no failure mode left now that no IANA name
  is resolved; the `finalise_*` functions are the last thing that runs and must not be able to fail.
- **Guarantees.** `finalise_error` calls `unwrap_cause` and asserts the resulting message is non-empty
  before returning (falling back to the exception's class name), which is invariant E3. **It preserves
  every field the protocol already populated** — `status_code`, `headers`, `cookies`, `text`, `json` —
  and only ever *adds* `error` and flips `ok`. It never resets the envelope to a failure skeleton,
  which is invariant E11.

### `utils/exceptions.py`

```
def unwrap_cause(exc: BaseException, *, max_depth: int = 10) -> tuple[str, str | None]
```

Walks `__cause__`, then `__context__`, to the deepest **non-empty** `str()`, bounded by `max_depth`
(so a cyclic chain terminates). Returns `(message, cause_description)`. Returns
`(type(exc).__name__, None)` when the entire chain is empty.

### `helpers/internal/breaker_registry.py`

```
def get_breaker(
    family: str,
    host: str,
    port: int,
    config: CircuitBreakerConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CircuitBreakerHelper
def reset() -> None            # test-only; documented as such
```

- Process-lifetime, bounded LRU (documented cap, default 256 destinations), keyed
  `(family, host, port)`. Eviction is by least-recent *use*, so a destination under active failure is
  never evicted while it matters.
- **`clock` / `sleep` are the timing seam** (see `CircuitBreakerConfig`) and are honoured by the
  **facade** `get_breaker` returns, never by the resilience library underneath it (Ruling D). They are
  keyword-only with real defaults, take precedence over the same keys in `config` when both are given,
  and are **not** part of the LRU key — substituting a fake clock in a test does not create a second breaker for the
  same destination. `reset()` clears the registry between tests so a fake clock never leaks into the
  next test's state.
- **Errors.** `ConfigurationError` for an invalid config (unknown key, wrong type).
- **Thread/loop safety.** The library is single-event-loop by assumption; the registry uses no locking
  and this is documented. If a consumer runs multiple loops in one process, breaker state is shared —
  documented as intended (the destination's health is a process-level fact).

### `utils/paths.py`

```
def resolve_within(base: Path, candidate: str | Path) -> Path
async def safe_write(path: Path, data: bytes, *, overwrite: bool = False) -> None
async def safe_unlink(path: Path) -> None      # idempotent
```

- `resolve_within` canonicalises (resolve + symlinks) and raises `PathContainmentError` when the result
  escapes `base`, when the candidate is absolute, or when it contains a null byte.
- `safe_write` opens with `O_NOFOLLOW` (and `O_EXCL` unless `overwrite=True`), restrictive mode, via
  `aiofiles`; raises `PathContainmentError` on a symlink target and `ConfigurationError` on an existing
  file with `overwrite=False`. On any failure it removes a partially-written file (M19).
- `safe_unlink` succeeds when the file is already absent (M19).

### `logic/soap_client.py`

```
class SoapRequest(BaseRequestClass):
    async def handle_request(self) -> GatewayResponse

def build_envelope(body: str | Element, *, version: str, headers: Element | None) -> str
def soap_transport_headers(*, version: str, action: str | None) -> dict[str, str]
def parse_soap_response(raw: str, *, version: str) -> tuple[Element | None, SoapFault | None]
```

- `soap_transport_headers` returns, for **1.1**: `{'Content-Type': 'text/xml; charset=utf-8',
  'SOAPAction': '"<action>"' or '""'}`; for **1.2**:
  `{'Content-Type': 'application/soap+xml; charset=utf-8' + ('; action="<action>"' if action else '')}`
  and **no** `SOAPAction` key.
- `parse_soap_response` rejects a `DOCTYPE` declared in the document **prolog** — before the root
  element's start tag — **before** calling `ElementTree.fromstring`, raising `UnsafeXmlError` (R19).
  **It is not a substring search over the body:** the literal text `DOCTYPE` inside a `<detail>`
  element, a CDATA section or a base64 field is legitimate content and is parsed normally. Implement
  by scanning the prolog or by driving an `expat` parser whose `StartDoctypeDeclHandler` raises;
  R19 requires one test for each direction (billion-laughs prolog rejected, in-element `DOCTYPE`
  accepted).
- `parse_soap_response` returns `(body_element, None)` on success and `(None, fault)` on a Fault, where
  **`body_element` is the first *element child* of `<Body>`** — not the `<Body>` element itself — and
  is `None` when `<Body>` is absent, empty, or the response body is empty. A `<Body>` with multiple
  element children returns the first and logs a `warning` with the count. *(The definition is stated
  here because it is observable: R18's "an empty response on HTTP 202 yields `soap_body is None`"
  criterion is only satisfiable under the first-child reading — an empty `<Body/>` is a perfectly good
  `Element`.)*
- `parse_soap_response` raises `SerializationError` on malformed XML or on a well-formed document that
  is not a SOAP envelope. The bytes it parses have already passed R14's `max_response_bytes` cap
  (R19's capped-read criterion); it does not read from the transport itself.
- `build_envelope` detects an already-complete `<Envelope>` and does not double-wrap. Its return value
  is what reaches the wire **byte-for-byte** (UTF-8 encoded): `SoapRequest` dispatches through R12's
  raw-body filter (`data=`), never through the JSON filter, and R18 asserts the equality against the
  raw body the loopback `aiohttp.web` handler received (`await request.read()`).

### `utils/redaction.py`

```
REDACTED = '***redacted***'

SENSITIVE_HEADERS: frozenset[str]      # authorization, proxy-authorization, cookie,
                                       # set-cookie, x-api-key  (compared casefolded)
SENSITIVE_NAMES: frozenset[str]        # api_key, apikey, access_token, refresh_token, token,
                                       # secret, password, passwd, signature, sig, key, auth
PAYLOAD_REDACTION_DEPTH: int = 4

def redact_headers(headers: Mapping[str, str]) -> dict[str, str]
def redact_url(url: str, *, extra_params: Collection[str] = ()) -> str
def redact_payload(payload: Any, *, depth: int = PAYLOAD_REDACTION_DEPTH) -> Any
def redact_value(value: Any) -> Any
```

One implementation used by **both** the envelope builder and the logger, so the two cannot drift.
Never raises — a redactor that can fail turns a logging call into an outage.

- `redact_headers` — replaces the *value* of any header whose casefolded name is in
  `SENSITIVE_HEADERS`. Header names are preserved so the caller can still see *that* an
  `Authorization` header was sent.
- **`redact_url`** — parses the URL, strips netloc **userinfo** (`https://u:p@host/` → `https://host/`),
  and masks the value of any query parameter whose casefolded name is in `SENSITIVE_NAMES ∪
  extra_params` (from `protocol_info['redact_query_params']`). Parameter order and every other
  component are preserved. An unparseable URL is returned unchanged rather than raising — the URL is
  diagnostic data, not a security boundary, and losing it hurts more than the residual risk.
- **`redact_payload`** — for mappings, replaces the value of any key whose casefolded name is in
  `SENSITIVE_NAMES`; recurses into nested mappings and sequences to `depth`. **Below `depth`, and for
  any non-mapping payload (`str`, `bytes`, a file body, an arbitrary object), the value is returned
  unchanged.** This bound is deliberate: unbounded recursion over caller data is both a performance
  hazard and a branch-coverage burden, and the honest thing is to state the limit in the README rather
  than imply a guarantee the function does not make. This is why invariant E9 is scoped the way it is.
- `redact_value` — the dispatcher used by the logger's `extra`: applies the right one of the above by
  type, so a value logged and the same value in the envelope are redacted identically.

---

## State management

The library is **stateless per request except for one thing**, and that exception is deliberate.

| State | Lifetime | Where | Why |
|---|---|---|---|
| The response envelope | Per request | Created by `request()`, filled by the protocol object, returned | It is the return value. It is never shared, never aliased into itself (E4), and never a caller-owned object that outlives the call. |
| Protocol object (`HttpRequest`, …) | Per request | Constructed in `request()` | Correct today and kept. It is the *breaker* that must not be per-request, not the protocol object. |
| **Circuit-breaker state** | **Per process, per destination** | `helpers/internal/breaker_registry.py` | This is the only genuinely cross-request state. It has to survive across calls or the breaker can never open (H8), and it has to be keyed per destination or one flaky host opens the circuit for all of them (M16). Bounded LRU so it cannot grow without limit. |
| aiohttp `ClientSession` / connection pool | Caller-owned when supplied, else per request | `protocol_info['session']` or created and closed by the library | Pooling requires a session that outlives one call (H11). The library will not hold a global session, because a library that owns an event-loop-bound resource across an application's lifecycle cannot be closed cleanly — so the caller owns it, and the default stays correct-but-unpooled with the cost documented. |
| Tracer results | Per request | aiohttp's trace `context` object | Today they live on the shared `TraceConfig` (H17); moving them to the per-request context is the fix. |
| Caller-supplied config dicts (`protocol_info`, `circuit_breaker_config`, `additional_arguments`, payload) | Caller-owned | **Copied on entry; never mutated** | Three separate findings (M6, M12, M28) are the same root cause. The rule is absolute: nothing the caller passes in is written to. |
| Logger configuration | Application-owned | The library attaches only a `NullHandler` | A library that configures logging fights its host. |

**Concurrency model.** The library assumes one event loop and makes no thread-safety claims beyond
what the underlying clients provide. This is stated in the README. The stated use case is
`asyncio.gather` over many `request()` calls sharing one `protocol_info` — which is exactly the case
M6/M12/M28 corrupt today, and which R12/R17/R24's "caller's dict is unchanged after two calls" tests
cover directly.

---

## Implementation steps

Ordered so that no step depends on a later one, and so that the fix-interaction constraints (FI-1 …
FI-16) are satisfied by the ordering rather than by anyone remembering them. Each step ends with a
verifiable check.

> **Two rules that apply to *every* step below, not just the ones that mention them.**
>
> 1. **The coverage ratchet (R28, Ruling 1).** Coverage is measured with
>    `--cov=async_gateway --cov-branch` from Step 1. **Every step ends by re-measuring and raising
>    `fail_under` to that step's measured value**, in that step's own commit. The value may never go
>    down — R1's CI check enforces it against the base branch. Step 1 sets the initial floor to
>    whatever is measured then; Step 26 drives it to exactly **100**. A step that adds code without
>    raising the number has not finished.
> 2. **Step numbering is stable — steps are inserted and vacated, never renumbered.** `Step 1.5` and
>    `Step 4.5` are *insertions*; **`Step 21` is *vacated*** (its work moved to Step 1.5) and retained
>    as an empty numbered slot. Nothing is renumbered, so every cross-reference in this document — the
>    FI-* table, the requirement-ordering table, "terminal 100 at **Step 26**", every criterion that
>    names a step — keeps pointing at the same work across three revisions of this plan. The decimal
>    follows the convention the project's own pipeline rules use for inserted stages (`1e.5`, `2a.5`,
>    `3b.5`); the vacated slot follows this spec's own `C5` precedent, where an id was emptied rather
>    than reused so that nothing citing it dangles.

### Phase 0 — Scaffolding (nothing else starts until this is green)

**Step 1 — Make the test suite capable of failing, able to run an async test, and *measured*.** *(R2, R28-ratchet · C8, M26, M27, H19)*
Add `pytest-asyncio` (`asyncio_mode = "auto"`) and **`pytest-randomly`**; resolve the
flake8 startup failure (`setuptools<81` or drop `flake8-import-order`); delete `tests/test_pass.py`;
delete `tests/coverage_output.py`; remove `-k pre_test`.
**Add the HTTP/SOAP fixture — a loopback `aiohttp.web` server via `aiohttp.test_utils.TestServer`
whose handler records the request it received.** No aiohttp-mocking library is added: `aioresponses`
is broken against the entire aiohttp 3.14.x line (see the decided-fact box under Assumptions), and it
is the fixture, not a dependency, that every HTTP and SOAP assertion in this plan is written against.
**Start the coverage ratchet here:** configure `--cov=async_gateway --cov-branch` and set `fail_under`
to the **then-measured** value (expected to be near 0 — that is the point; it is a floor that can only
rise, not a target). Add R1's ratchet check to CI in Step 3.
*Verify:* in a fresh venv, `flake8 --version` exits 0; an `async def` smoke test runs; a deliberately
failing test makes `pytest` exit non-zero; `pytest -p randomly --randomly-seed=1` exits 0; `pytest`
prints a branch-coverage total and `fail_under` equals it.
***And the check that would have caught the plan critique's Critical on day one:*** in a scratch venv
carrying the dev set **plus `aiohttp>=3.14.3,<4`** — the version Step 1.5 is about to declare, not the
`~=3.9.5` still pinned right now — drive one request end-to-end through the fixture and assert the
recorded request. This is the executable form of the standing constraint under Assumptions, and it is
deliberately *before* the upgrade: it proves the test architecture works on the target transport
**before** twenty steps of tests are written against it.
*FI-5 is satisfied here — `coverage_output.py` is deleted in the same step that enables `--cov-branch`,
so `int("0.5432")` never runs.*

**Step 1.5 — Upgrade the dependency set. This closes C7, in Phase 0, before any test is written.** *(R6 · C7, H23 · FI-10, FI-11, FI-12 · orchestrator Ruling B)*
Move every runtime dependency in `requirements.txt` to the pre-approved version, expressed as a range
with a major-version ceiling: `aiohttp>=3.14.3,<4`, `orjson>=3.12.0,<4`, `aioboto3>=15.5.0,<16`,
`aiofiles>=25.1.0,<26`, `aioftp>=0.28.0,<1`, `asyncssh>=2.24.0,<3`. **Two deliberate exceptions, each
owned by a later step:** `pytz` stays declared at `>=2026.3.post1,<2027` until **R9 (Step 5)** removes
its import, and `requests` stays until **R3 (Step 4)** drops it — removing either here would turn the
clean-venv import red for a module that still imports it.
**No aiohttp API migration happens here.** The three surfaces the 3.9 → 3.14 range touches are
*rewritten* against this set by R14 (Step 13), R23 (Step 15) and R26 (Step 19), and the S3 client by
R25 (Step 18) — each once, each with its own tests. Executed evidence that the existing code survives
the interval: on aiohttp 3.14.3, `verify_ssl` and `ssl` are both still present on
`ClientSession._request` and `TCPConnector.__init__`, and the `TraceConfig` callbacks are intact.
*Why this is Phase 0 and not Step 21:* see FI-10 and "Said out loud: C7 now closes in Phase 0". In
short — the migration risk the old deferral hedged is small and the surfaces are being rewritten
anyway, while the deferral's own cost (an entire test architecture built on a transport the release
then replaces, revalidated under a non-lowerable coverage ratchet at step 21 of 31) is what turned one
false assumption into a project-stopping event.
*Verify:* a fresh venv resolves the upgraded set with no backtracking warning; **the clean-venv
submodule import (`from async_gateway.async_gateway import request`) succeeds against the source
tree** — this is the check that catches a module-level import broken by `aioboto3` 15.x or `asyncssh`
2.24 (FI-11), at the cheapest possible moment, and its artifact-level form arrives with Step 2/Step 3;
Step 1's fixture round-trip passes against the now-declared aiohttp rather than a scratch install; and
the dependency advisory scan reports **zero Critical/High on the resolved set** (an absolute condition,
never a delta against the audit's "38 in 3 packages" — FI-12).
*Contingency, stated so it is not improvised:* if the import goes red because `aioboto3` 15.x no longer
satisfies a module-level import, R25's S3 call-site repair is pulled forward into this step as the
minimum needed to keep Phase 0 green; the rest of R25 stays at Step 18.

**Step 2 — Declarative packaging.** *(R4 · M22, M23, H26, L16, L17)*
Create `pyproject.toml` (PEP 517 + 621) with dependencies, `requires-python`, truthful classifiers, and
the sdist file set. **The dependency table transcribed here is the Step-1.5 set** — the target versions
are written down once, never as old pins that a later step amends. Delete `setup.py`, `setup.cfg`,
`requirements*.txt`. **Do not add `py.typed`.**
*Verify:* `python -m build` produces both artifacts; `twine check dist/*` passes; the sdist contains
`README.md`, `LICENSE`, `pyproject.toml`; `pip install --no-binary :all: dist/*.tar.gz` succeeds in a
clean venv.

**Step 3 — CI.** *(R1 · H21)*
Build → install the **wheel** into a clean venv → `from async_gateway.async_gateway import request` →
repeat for the **sdist** → `flake8` (with a baseline suppression file) → `mypy` (still with
`ignore_errors`) → `pytest` → `bandit -r async_gateway -ll` → `twine check`. Also here: the
**interpreter matrix** — the committed list `3.10`, `3.11`, `3.12`, `3.13`, **`3.14`**, with the test
asserting it is ≥ the `requires-python` floor and contiguous, plus the scheduled job that fails when a
newer stable minor exists — the **shuffled-order job** (`pytest -p randomly` over five committed
seeds), and the **coverage-ratchet check** (`fail_under` on head ≥ `fail_under` on base).
CI stands up against the **Step-1.5 dependency set**, so it has never been green on the un-upgraded one.
*Verify:* the workflow is green on every interpreter in the matrix, 3.14 included; a scratch branch that
re-introduces an undeclared import turns the clean-venv job red; a scratch branch that lowers
`fail_under` turns the ratchet job red; a scratch branch that deletes `3.14` from the matrix turns the
contiguity test red.

**Step 4 — Finish the `orjson` migration.** *(R3 · C1, MG2 · FI-6)*
Migrate **all four** `ujson` sites — three direct call sites across **three** modules
(`response_helper.py:14`, `filters_helper.py:45`, `filters_helper.py:78`) plus the `json_serialize`
wiring at `logic/http.py:30`; **wrap the default `json_serialize` to return `str`**; decode at both
`filters_helper` sites; drop `requests` from the dependency set (Step 1.5 deliberately left it there).
*Verify:* `grep -rn ujson async_gateway/` is empty; the Step-3 clean-venv import job, which was failing
in reality even though it passed in CI's pre-install environment, now passes against a truly clean
venv; a JSON POST driven through the Step-1 fixture arrives at the handler with the expected body.

**Step 4.5 — Rename the protocol modules, before anything is written against the old names.** *(R27's A005 · L15-adjacent · FI-15 · blocked on **OQ9**)*
`git mv` `logic/http.py` → `logic/http_client.py`, `logic/ftp.py` → `logic/ftp_client.py`,
`logic/sftp.py` → `logic/sftp_client.py`; delete the 0-byte `logic/soap.py` (R18 creates
`logic/soap_client.py` under its final name at Step 20); update the three import sites in
`logic/__init__.py` and any other importer. **No behaviour changes in this commit** — it is a pure
rename so the diff is reviewable as one.
*Why here and not at Step 24:* every test module this plan creates from Phase 2 onward is named
`tests/logic/test_http_client.py` / `test_ftp_client.py` / `test_sftp_client.py`; R28 requires tests to
mirror the source structure; and the traceability rows for R3, R13 and R14 name `logic/http_client.py`
as the file they modify. Renaming at the end would mean twenty steps of work written against filenames
that do not exist, then the largest and least reviewable diff in the plan landing in the release phase.
There are zero consumers (A4) and no compatibility alias is added, per "delete the path you superseded".
*Verify:* `flake8` reports zero `A005`; `grep -rn "logic/http\.py\|logic/ftp\.py\|logic/sftp\.py\|logic/soap\.py" .`
returns nothing outside this spec and the audit report; the Step-3 clean-venv import job is still green.
*Blocked on OQ9* — the rename is a human-ratifiable decision and it now sits in Phase 0, so OQ9 must be
answered before implementation begins.

### Phase 1 — The contract (everything after this is written against it)

**Step 5 — Envelope, timestamps, exceptions, logger.** *(R8, R9, R10 · C2, H7, H27, H29, M20, MG3, L3, L4, L11, L12 · FI-3, FI-7)*
Create `utils/envelope.py`, `utils/status_map.py`, `utils/redaction.py` (with `redact_headers`,
`redact_url`, `redact_payload`, `redact_value`); rewrite `utils/exceptions.py` with the hierarchy and
`unwrap_cause`; rewrite `date_helper` on stdlib `datetime` UTC + `time.monotonic` (no `pytz`, no
`zoneinfo`) **and drop `pytz` from the dependency set in this same commit** — Step 1.5 deliberately
left it declared because `date_helper` still imported it, and import and declaration must go together
or the clean-venv job breaks in one direction or the other; attach the `NullHandler` in `__init__.py`
and add per-module loggers.
**Close FI-7's HTTP half in this step, not later:** delete `helpers/internal/request_helper.py:93`'s
`response = kwargs.get('response', {})`; `make_http_request` returns a typed `HttpResult` and
`logic/http_client.py` copies it into the envelope it was handed. This is the seam fix — doing it in
Step 11 or 13 would mean the envelope contract lands with one protocol still building its own response
shape, which is exactly the state FI-7 describes.
*Verify:* `test_envelope.py` and `test_exceptions.py` pass, including **E1–E11** and the
never-empty-message test with a `RetriesExhausted() from ClientConnectorError` chain;
`grep -rn "kwargs.get('response'" async_gateway/` returns zero; `fail_under` is raised to the measured
value.

**Step 6 — Dispatch and validation.** *(R11 · H3-registration, H4, H5, H6, L2 · FI-14)*
Normalise the protocol once; typed registry; scheme enforcement after pre-processors; `protocol_info`
guard; the exception→envelope boundary in `request()`.
*Verify:* `test_entrypoint.py` passes the dispatch matrix; a deliberately-injected `KeyError` inside a
protocol handler **propagates** out of `request()`.

**Step 7 — Write the contract tests before the protocol work.** *(R28-partial)*
The ~20-line no-network dispatch/envelope tests across all five protocols × {success, failure}. They
will fail for FTP/SFTP/SOAP; that is the point — they are the acceptance criteria for Steps 8–12.

### Phase 2 — Protocol correctness

**Step 8 — FTP, all three fixes in one commit.** *(R15 · C6, H1, M2, M3, H10-ftp · FI-1)*
**Step 9 — SFTP transport security, with the fixture written verification-on.** *(R16 · C4 · FI-9)*
**Step 10 — SFTP operation correctness.** *(R17 · H2, M4, M28, L5, L6, H10-sftp)*
**Step 11 — HTTP request construction and response handling.** *(R12, R13 · H14, M5–M10, L8)*
**Step 12 — Blocking I/O → `aiofiles`, plus the AST lint test.** *(R20 · C3)*
*Verify each:* the step's own tests plus Step 7's contract tests for that protocol go green, and the
AST no-blocking-I/O test passes for the whole package after Step 12.

### Phase 3 — Resilience, security and utilities

**Step 13 — HTTP transport resilience, including the owned redirect loop.** *(R14 + R21's per-hop scheme check · H9, H10-http, H11, H12, MG4, M21-in-scope-half · FI-16)*
Caller-suppliable session, explicit `timeout=` everywhere, the 64 MiB `max_response_bytes` cap on every
read path, the per-attempt body factory, the 64 KiB chunk default — **and** `allow_redirects=False` on
the transport call with the library following redirects itself, re-checking every `Location` against
`allowed_schemes` before issuing the next request. The redirect controls exist for that check
(Ruling 4 / EM-H2); they are not a general redirect-configuration feature.
*Verify:* a 302 to `ftp://` is refused with `error.code == 'CONFIG'` and **no second transport call**
is made; a 302 to a relative `Location` resolves and is followed; `max_redirects + 1` hops fails.
**Step 14 — Circuit breaker + per-destination registry, together, with the clock/sleep seam.** *(R24 · H8, M12–M16, L9, L10 · FI-4)*
The registry, the typed config, **and** `clock`/`sleep` threaded through `get_breaker` — the seam
R28's no-sleep rule depends on. Every timing test written here drives the seam.
**The seam is implemented in this library's own breaker facade, not requested from `pyfailsafe`**
(Ruling D): `pyfailsafe` provably has none (`circuit_breaker.py:139,142`, `failsafe.py:103`), so the
facade owns the whole breaker state machine and the whole retry loop — failure counting, the state
transitions, the callback invocations and the backoff wait — and delegates **only** exception
classification and the backoff computation downward. Because the facade's
interface is the same under keep, replace and vendor, **this step does not wait on Step 22** — which
is what takes Step 26's terminal coverage gate off an open dependency decision.
*Verify:* a full open → half-open → close cycle runs on a `FakeClock` and a recording `sleep`, with
`time.monotonic` and `asyncio.sleep` untouched.
**Step 15 — TLS and client certificates, with a live local-handshake test.** *(R23 · H28, M1 · FI-2)*
**Step 16 — Path containment and safe local file handling.** *(R22 · M17, M18, M19)*
**Step 17 — Verb allowlists and the URL-trust contract.** *(R21 · M25, H13-pointer, M21-partial)*
**Step 18 — File-transfer utilities and dead-code removal.** *(R25 · H15, H16, MG5, L13)*
**Step 19 — Request tracer.** *(R26 · H17, M11, M20-part)*

### Phase 4 — SOAP

**Step 20 — `logic/soap_client.py`.** *(R18, R19 · H3)*
Envelope construction, version-correct headers, hardened parse, Fault mapping. Registered in Step 6's
registry, so `test_entrypoint.py`'s SOAP rows go green here.
*Verify:* both versions round-trip; a Fault with HTTP 200 yields `ok=False`; a billion-laughs envelope
is rejected before `fromstring` is called.

### Phase 5 — The resilience-library decision

**Step 21 — *vacated*.** *(was: R6, the dependency upgrade)*
The work that lived here **moved to Step 1.5, in Phase 0** (orchestrator Ruling B, closing the plan
critique's Critical). The number is retained and left empty rather than reused or renumbered, on the
same principle as the vacated `C5` finding id: everything that cited "Step 21" — the FI-* table, R23's
edge cases, C7's traceability row, the withdrawn "Said out loud" disclosure — now points somewhere
truthful, and nothing silently re-binds to different work. **There is no Step 21.**

**Step 22 — The resilience-library decision.** *(R7 · MG1 · OQ12)*
Run fitness **behaviours 1–6** from Step 14 against `pyfailsafe` on the Step-1.5 dependency set,
**across every interpreter in the R1 matrix** (`3.10`–`3.14`) — dormancy bites at the ceiling, not the
floor. **Behaviour 7 is not re-evaluated here: it is already known-failed by execution** (no injectable
clock or sleep — `circuit_breaker.py:139,142`, `failsafe.py:103`), and its three-way proposal is
already resolved by Ruling D as **keep-with-named-cost**, with R24's Step-14 facade holding the seam.
This step's job is therefore behaviours 1–6, the costing, and the ADR — not an open-ended decision
sitting four steps upstream of Step 26.
*Verify:* behaviours 1–6 pass on every interpreter in the matrix against whatever is kept; the matrix
output and the behaviour-7 grep are both pasted into the ADR; the ADR records why keep-with-wrapper
beat *replace* and *vendor*, the named cost **verbatim** (the facade owns the whole breaker state
machine and the whole retry loop; the dependency supplies exception classification and backoff
computation only — `retry_policy.py`, 136 of 539 lines, ~40 non-trivial), the owner and the revisit
trigger, and the human's answer to **OQ12**; if the costing shows the residual smaller still — the
facade having to reimplement classification or backoff computation as well — the ADR says so and
recommends vendoring instead; if a breaker was vendored it is ≤ 250 lines at 100% branch coverage.

### Phase 6 — Gates on, one per commit

**Step 23 — Docstrings and full type annotations.** *(R30 · L1, L2, L4, L5, L7, L14 + annotation coverage · FI-8)*
Deliberately **before** the docstring lint rules are un-suppressed, so no large baseline is created for
a class of finding that is about to be fixed wholesale.

**Step 24 — flake8 to zero.** *(R27 · H19, L15)*
Fix `application_import_names` (`async_gateway`, underscore — an illegal identifier today, so
import-order checking has been checking a fiction); shrink the baseline suppression file to zero and
delete it; choose and enforce one quote style so the 938 quote-preference findings become a formatter
concern rather than a lint backlog.
*The `A005` rename is **not** here — it happened at Step 4.5 (FI-15).* This step **verifies** it:
`flake8` reports zero `A005` with no `# noqa` anywhere.

**Step 25 — Remove `mypy ignore_errors`; drive to zero; then add `py.typed`.** *(R27, R4 · H18 · FI-13)*

**Step 26 — Coverage ratchet to its terminal value: 100% line + branch, enforced.** *(R28 · H20)*
The ratchet has been rising since Step 1; this step drives it to exactly **100** and adds the two
pragma CI checks (justification comment required; total pragma count ≤ **10**).
*Verify:* `pytest` exits 0 at 100/100 line+branch on a clean checkout and non-zero at 99.9%; the
pragma count is ≤ 10 and every pragma carries a `--` justification; the socket-blocked run passes;
`pytest -p randomly` passes on all five committed seeds.
*This step is a **finish**, not a flip.* If `fail_under` is still far below 100 when Step 26 begins,
the ratchet was not being maintained and that is a process failure to escalate — not a number to
negotiate down.

### Phase 7 — Release

**Step 27 — Version, provenance, docs scaffolding.** *(R5, R31, R34 · H22, H25, M24 · OQ4, OQ7)*
**Step 28 — README rewrite, with the compile/import/signature tests.** *(R29 · H24, MG6, MG8)*
**Step 29 — `CLAUDE.md` commands block** *(R32 · MG7 — **blocked on OQ8 approval**)*
**Step 30 — CHANGELOG and examples.** *(R33, R35)*
**Step 31 — Final gate run and the tag.** *(R1, R4, R5)*
*Verify:* the five-item PR gate passes on a clean checkout; `v1.0.0` is tagged; nothing is uploaded.

---

## Error-handling design

### The one rule

**Protocol code raises. One place converts.** Every module below `async_gateway.py` raises a typed
`AsyncGatewayError`; `request()` is the single place that catches it and turns it into an `ok=False`
envelope. Nothing else catches broadly, and nothing else builds a failure response.

This replaces three identical blanket handlers (`http.py:65`, `ftp.py:68`, `sftp.py:62`) that
collapsed TLS rejections, DNS failures, timeouts, open circuits, and every `UnboundLocalError`,
`KeyError` and `TypeError` in the library's own code into one undifferentiated `999` with no log line.

```python
# async_gateway.py — the ONLY conversion point
started = time.monotonic()
try:
    envelope = await protocol_obj.handle_request()
except AsyncGatewayError as exc:
    # Ruling I / Rev 4: NO exc_info. A redacted traceback string instead — exc_info hands the
    # live chained exception to the app's handler, where no redactor of ours can run.
    logger.error("gateway request failed",
                 extra={"protocol": protocol, "url": redact_url(url), "code": exc.code,
                        "traceback": redact_text(format_exception(exc))})
    # finalise_error ADDS error + flips ok. It never clears status_code, headers,
    # cookies, text or json — the protocol populated those before raising (E11).
    envelope = finalise_error(envelope, exc, started=started)
# every other exception propagates — deliberately, and tested
```

### The rule's one obligation on the raiser (invariant E11)

"Protocol code raises" must not be read as "protocol code raises *instead of* filling in the envelope".
For a **remote-status** failure — an HTTP 4xx/5xx, a SOAP Fault at any transport status, an FTP or SFTP
status error — the response genuinely exists and the caller almost always needs it: a `404` with a JSON
error body is the single most common failure a consumer handles. So the sequence is fixed:

```python
# logic/http_client.py — every remote-status failure, without exception
result = await make_http_request(...)          # -> HttpResult
copy_into_envelope(self.response, result)      # status_code, headers, cookies, text
self.response['json'] = parse_body(result)     # response_helper parses text/body — not an HttpResult field
if result['status_code'] >= 400:
    raise HttpStatusError(...)                 # the envelope is ALREADY populated
```

An exception-only control flow that skips the copy returns `text=''`, `json=None` on every error
status. That is a regression against today's behaviour and it would be invisible to the E1 key-set
test, which only checks that the *keys* are present. Hence E11, and hence R8's named test.

### What each failure mode does

| Failure | Raised by | Type | `status_code` | `ok` | Logged | Retryable? |
|---|---|---|---|---|---|---|
| Unknown / `None` / non-string protocol | `request()` | `ConfigurationError` | — (**raises**) | — | — *(raises; never logged — single-report principle, Rev 4)* | No |
| Missing required `protocol_info` key | `BaseRequestClass.__init__` | `ConfigurationError` | — (**raises**) | — | — *(raises; never logged — single-report principle, Rev 4)* | No |
| `HTTPS` + `http://` URL | `request()` | `ConfigurationError` | — (**raises**) | — | — *(raises; never logged — single-report principle, Rev 4)* | No |
| Unknown verb / unknown breaker key | allowlist / config parser | `ConfigurationError` | — (**raises**) | — | — *(raises; never logged — single-report principle, Rev 4)* | No |
| `auth` carrying no `login`/`password`, on FTP or SFTP | `base.credentials_of`, from the protocol constructor | `ConfigurationError` | — (**raises**) | — | — *(raises; never logged — single-report principle, Rev 4)* | No |
| FTP `command`/`server_path`, SFTP `mode`/`remote_path` — absent, malformed, or outside the allowlist | `ftp_client._validate_request`, `sftp_client._validate_mode` | `ConfigurationError` / `UnsupportedVerbError` | 400 | False | `error` | No |
| DNS failure | `http_client` | `DnsError` | 502 | False | `error` + redacted `traceback` | Yes (transient) |
| Connect refused / reset | `http_client`, `ftp_client`, `sftp_client` | `ConnectError` | 502 | False | `error` + redacted `traceback` | Yes |
| TLS handshake / cert failure | `filters_helper`, protocol clients | `TlsError` | 502 | False | `error` + redacted `traceback` | No (config) |
| SSH host-key mismatch | `sftp_client` | `HostKeyError` | 495 | False | `error` + redacted `traceback` | No (config) |
| Connect / read / total timeout | all protocols | `GatewayTimeoutError` | 504 | False | `error` + redacted `traceback` | Yes |
| Circuit open | breaker | `CircuitOpenError` | 503 | False | `warning` | Later |
| HTTP 4xx / 5xx | `http_client` | `HttpStatusError` | the real status | False | `warning` | Depends |
| SOAP Fault (incl. HTTP 200) | `soap_client` | `SoapFaultError` | the real status | False | `warning` | Depends |
| FTP reply 4xx / 5xx | `ftp_client` | `FtpStatusError` | the reply code | False | `warning` | Depends |
| SFTP `SSH_FX_*` | `sftp_client` | `SftpStatusError` | 404 / 403 / 500 | False | `warning` | Depends |
| *(all four rows above)* | — | — | — | — | — | **The envelope carries the full response — `text`, `json`, `headers`, `cookies` — populated *before* the raise. Invariant E11.** |
| Redirect target outside `allowed_schemes` | `request_helper` (owned redirect loop) | `ConfigurationError` | 400 | False | `error` | No |
| Response over cap | `request_helper` | `ResponseTooLargeError` | 502 | False | `error` | No |
| Body will not parse | `response_helper`, `soap_client` | `SerializationError` | 502 | False | `error` | No |
| Request body will not serialise | `filters_helper` | `SerializationError` | 400 | False | `error` | No |
| XML with a `DOCTYPE` | `soap_client` | `UnsafeXmlError` | 502 | False | `error` | No |
| Path escapes the target directory | `utils/paths.py` | `PathContainmentError` | 400 | False | `error` | No |
| Pre/post-processor raises | `request()` | wrapped `ConfigurationError` | 400 | False | `error` + redacted `traceback` | No |
| **Library bug** (`KeyError`, `TypeError`, `UnboundLocalError`, …) | anywhere | **propagates unchanged** | — | — | not caught | — |
| `asyncio.CancelledError` | anywhere | **propagates unchanged** | — | — | not caught | — |

### Logging contract

- One logger tree, `async_gateway.*`, with a `NullHandler` on the root of it and **no other handler**.
- Every failure logs exactly once, at the conversion point, with a **redacted `extra['traceback']`
  string** (**not** `exc_info=True` — see Revision 4 / Ruling I) and structured
  `extra={'protocol', 'url', 'code', 'status_code', 'latency', 'traceback'}` — never f-string
  interpolation into the message.
- **An error is reported exactly once** (Revision 4 / OQ14, single-report principle): either it
  escapes to the caller as a `raise` — the four pre-dispatch `(raises)` rows below, which log
  **nothing** — or it is converted to an envelope inside `handle_request`, and *that* conversion
  point logs it. There is no third behaviour: a path never both raises and logs.
- `warning` for a remote-side failure the caller may legitimately expect (4xx, a Fault, an open
  circuit); `error` for a transport or configuration failure.
- Every **scalar** value passing through `extra` goes through `redact_value` first — which dispatches by type to
  `redact_url` for the URL, `redact_headers` for a header mapping, and `redact_payload` for the payload
  — so the logger and the envelope cannot disagree about what is a secret. In particular the `url` key
  is `redact_url(url)`, not the raw string: a URL carrying `?api_key=` in the envelope but in the clear
  in the log would defeat the whole point of sharing one redactor. The one exception is `traceback`,
  which goes through `redact_text` alone: `redact_value` adds a whole-string `redact_url` pass, and that
  pass would read the prose *after* the first URL in a multi-line trace as part of the query it is
  masking and truncate the trace there.

### Consolidated edge-case mapping

Each spec-side edge case has an implementation home. Grouped by class rather than repeated per
requirement.

| Edge-case class | Approach | Where |
|---|---|---|
| **Null / absent config** (`protocol=None`, `protocol_info=None`, missing `command`/`mode`/`request_type`, `auth=None` on FTP/SFTP) | Validate once at the boundary; raise `ConfigurationError` naming the missing key. Never `AttributeError` on `None.lower()`. | `async_gateway.py`, `base.py` (R11) |
| **Empty values** (`data={}`, empty body, empty directory listing, zero-byte upload, `trace_config=[]`) | Each is a legitimate value with a defined, distinct result — never conflated with an error. `json={}` ≠ parse failure; `files=[]` ≠ "not a directory". | R13, R17, R26 |
| **Case and whitespace** (protocol, verb, `request_type`, header names, media types) | One normalisation helper, applied at the boundary; every lookup is case-insensitive. | R11, R12, R21 |
| **Boundary numerics** (`maximum_failures=0`, `delay=1.5`, `jitter=True` passed as `1`, chunk size 0, `max_redirects=0`) | Typed config with explicit `isinstance` checks that accept `int`/`float` and reject `bool`; `0` is reachable. | R24 (L9, L10, M15) |
| **Oversized inputs** (100 MB response, a multipart part 3× the chunk size, a billion-laughs envelope) | Hard byte cap enforced before and during the read; chunked accumulation into a buffer, never `str + str`; DOCTYPE rejected pre-parse. | R14, R13, R19 |
| **Concurrent access** (`asyncio.gather` over one shared `protocol_info`; one shared `TraceConfig`; two requests to one destination) | Copy every caller dict on entry; per-request trace context; a process-level breaker registry keyed per destination with no shared per-request state. | R12, R17, R24, R26 |
| **Hostile remote input** (server-supplied entry names with `../`, a symlink at the target path, an HTML error page where XML was expected, a 404 body saved as a file) | `resolve_within` before every write; `O_NOFOLLOW`; parse failures are errors, not empty results; all non-success statuses refuse to write. | R22, R19, R25 |
| **Clock anomalies** (NTP step, keep-alive baseline overwrite) | `time.monotonic()` for durations, wall clock only for the displayed timestamp; the tracer records relative deltas throughout. | R9, R26 |
| **Partial failure** (mid-stream download error, retry after a completed destructive operation, processor raises after a successful call) | `try/finally` removes partial files; the operation's own success is recorded before any follow-up call (no unconditional post-`remove` `stat`); a processor failure is reported without discarding the completed call's result. | R22, R15, R17, R8 |
| **Empty error messages** (`str(RetriesExhausted()) == ''`) | `unwrap_cause` walks the chain and falls back to the class name; asserted by invariant E3. | R10 |
| **Platform differences** (`O_NOFOLLOW` unavailable, no `~/.ssh/known_hosts`) | Explicit degradation with an actionable message naming the missing thing; never a silent weakening of the security property. *(The "no system tz database" case is **designed out** rather than handled: R9 resolves no IANA name, so there is no `tzdata` branch to degrade — see OQ11.)* | R22, R16 |
| **Hostile redirect targets** (a 302 from `https://` to `ftp://`, `file://`, or plain `http://`; a relative `Location`; an unbounded redirect chain) | The library owns the redirect loop; every hop's resolved target is re-checked against `allowed_schemes` **before** the next request is issued, and the chain is bounded by `max_redirects`. | R14, R21 (FI-16) |
| **A response body on a failure status** (a 404 with a JSON error payload, a 500 with an HTML page) | The protocol populates the envelope's body/headers/cookies/status **before** raising the status error; `finalise_error` only adds `error` and flips `ok`. Invariant E11. | R8, R13 |

---

## Testing strategy

**The problem this has to solve:** 100% line **and** branch coverage on a library whose every
meaningful unit is `async def` and whose entire job is network I/O — with no test touching the network.

### Mocking, per boundary

| Surface | Mocked with | What the test asserts |
|---|---|---|
| HTTP / SOAP | **A loopback `aiohttp.web` server via `aiohttp.test_utils.TestServer`** — *not* a mocking library (Ruling A; `aioresponses` is broken against aiohttp 3.14.x, see the decided-fact box under Assumptions). A `conftest.py` fixture starts a server whose handler **records** every request (method, path, headers, raw body) and returns a caller-specified status/headers/body. | status, headers, body; **each redirect hop as its own route** (the library follows redirects itself, so every hop is a recorded request — including the one it must refuse, asserted as "the recorded request count stayed at 1"); and — critically — **the request the library actually sent**, read off the server rather than off a mock's ledger (URL, method, headers, `await request.read()` body bytes; for SOAP, byte-equality against `build_envelope`'s output). **Timeouts** are produced by a handler that awaits an `asyncio.Event` the fixture releases in teardown, with the library's `timeout` set to tens of milliseconds — the test waits only on the deadline it is asserting, which is the behaviour under test and *not* the sleep-to-settle pattern R28 forbids. |
| FTP | `aioftp.Client.context` patched with an async-context-manager double | the verb resolved, the arguments passed, `ssl=` never `None` when `verify_ssl`, `connection_timeout` present, `stat` not called after `remove` |
| SFTP | `asyncssh.connect` patched with an async-CM double yielding a fake SFTP client | **`known_hosts` is not passed as `None`**, `client_keys=[]` by default, `connect_timeout`/`login_timeout` present, typed `SFTPAttrs` consumed, caller's `additional_arguments` unchanged |
| S3 | `aioboto3.Session` patched | the exact `download_file(Bucket=, Key=, Filename=)` call signature |
| TLS / mTLS | a **real** local `ssl` server on `127.0.0.1` with a generated throwaway cert pair | a genuine client handshake succeeds through `get_ssl_config`, and `ctx.check_hostname` / `CERT_REQUIRED` hold. *(This is the one "network" test, and it is loopback-only — it exists because H28's path has never once connected and mocking it would prove nothing.)* |
| Filesystem | `tmp_path` | files written, not written, and cleaned up |
| Clock | **the injected seam, not a monkeypatch**: `CircuitBreakerConfig.clock` / `.sleep` (and `get_breaker`'s keyword-only equivalents) receive a `FakeClock` (an advanceable counter) and a recording `sleep` that returns immediately | breaker half-open timing, retry spacing and de-synchronisation, negative-latency resistance — **no `sleep`-based waiting anywhere, and no module-global patching**. The one exception is `time.monotonic` inside `date_helper` for the negative-latency test, which has no config to inject into and is patched directly. |

A socket-blocking check runs the whole suite with **outbound** network disabled. **`127.0.0.1` is
allowlisted**, because after Ruling A two fixtures bind loopback sockets: the `aiohttp.web` HTTP/SOAP
server and R23's TLS handshake server. Nothing in the suite may reach a host that is not loopback, and
that — not "no sockets at all" — is the property R28's criterion states.

### Reaching branch coverage on the hard shapes

Branch coverage, not line coverage, is where an async library leaks. The named traps:

- **`async for` loops** need a zero-iteration case, a one-iteration case, and a mid-loop failure.
- **`async with` / `try/finally`** need both the normal exit and the exception exit, or the `finally`
  branch is covered while the exceptional path is not.
- **Async generators** (the upload body factory) need exhaustion, early close, and re-invocation —
  which is exactly H9's defect, so the coverage requirement and the correctness requirement coincide.
- **`except` clauses** need one test per exception *family*, not one per `except` block.
- **Every `if` guarding a caller-optional key** needs present, absent, and present-but-`None`.
- **Both sides of every default** (`info.get('x', DEFAULT)`) — the audit's L9/L10 findings are exactly
  the branches nobody exercised.

Parametrised tables (`@pytest.mark.parametrize`) drive the repetitive ones — the protocol × outcome
matrix, the traversal table, the numeric-boundary table, the media-type table — so adding a case is one
line and a failure names the row.

### `# pragma: no cover` policy

Permitted **only** for:
1. `if TYPE_CHECKING:` blocks,
2. `@abc.abstractmethod` bodies,
3. platform-guarded fallbacks that cannot execute on the CI platform (the `O_NOFOLLOW` alternative).

Every pragma carries a `--` justification: `# pragma: no cover -- TYPE_CHECKING only`. Two CI checks
enforce it: one greps for any pragma lacking `--` and fails; one asserts the total pragma count does
not exceed a committed ceiling, so the policy cannot be used to buy the last few percent.

### Test-to-spec linkage

Each test module names the requirement ids it covers in its module docstring, and each test that
implements a named acceptance criterion references that criterion. This is what makes the Tester's and
the Merge Reviewer's job mechanical: the traceability table below says which files implement a
requirement, and the tests say which criteria they prove.

---

## Non-functional requirements

| Category | Requirement | How it is verified |
|---|---|---|
| **Event-loop safety** | No blocking call on any async path. One large transfer must not stall other in-flight work in the consuming process. | The AST test (R20) plus the loop-responsiveness test under `asyncio.gather`. |
| **Latency overhead** | The library's own overhead (envelope construction, redaction, validation, breaker lookup) is negligible against network time. No per-request work is O(response size) beyond the single decode. | The quadratic multipart accumulator is removed (M7); a micro-benchmark is *not* required — this is a bound on shape, not a number. |
| **Memory** | Peak resident memory for a request is bounded by `max_response_bytes` plus one decode copy, not by what the server chooses to send. | R14's over-cap test. |
| **Connection efficiency** | Callers can supply a session and get real pooling; the default is documented as unpooled with its cost stated. | R14's reuse test via the tracer. |
| **Transport security** | TLS verification on by default on every protocol. SSH host keys verified by default. Client certs use a client context. Every bypass is opt-in by name and logs a warning. | R15, R16, R23 tests; and the absence of `known_hosts=None`, `Purpose.CLIENT_AUTH`, and `or True` from the tree. |
| **Secret hygiene** | No credential value carried in a header, a cookie, an `auth` object or the URL (userinfo / sensitive query parameter) appears in the returned envelope, in any log record, or in the trace results. The payload echo is key-name-masked to depth 4; below that the caller's own data is echoed verbatim **and the README says so**. | Invariant E9 (scoped to exactly what `utils/redaction.py` delivers), the logger-redaction test (R10), the tracer test (R26), and the four-secret test in R8. |
| **Failure transparency** | Every failure is distinguishable by a wire-stable code, carries a non-empty message, is logged exactly once, **and — for a remote-status failure — still carries the response body**. | Invariants E2/E3/**E11** plus the failure-mode table's per-row tests. |
| **Observability** | The library emits structured logs through a `NullHandler`-rooted tree it does not configure, and returns per-request timing the consumer can chart. | R10, R26. *(There is no deployable surface, so SLOs, health checks and alerting do not apply; the Observability gate scope for this change is the logging contract.)* |
| **Supply chain** | Zero Critical/High advisories in the resolved dependency set; no exact pins forced on consumers; no new runtime dependency without a `library-review` and user approval. | R6, R7. |
| **Type safety** | Zero mypy errors with no `ignore_errors`; `py.typed` published so downstream checkers see it. | R27, R4. |
| **Maintainability** | 100% line + branch coverage; zero lint violations; zero unjustified suppressions; every module and public function documented. | R27, R28, R30. |
| **Portability** | Supported on the declared `requires-python` range, verified by a CI matrix; platform-specific fallbacks degrade explicitly, never silently. | **R1's matrix criterion** — a job per released minor interpreter in the declared range, **`3.10`–`3.14`**, with a test asserting the list is ≥ the floor and contiguous plus a scheduled job that fails when a newer stable minor appears. Revision 1 claimed a matrix no requirement created; Revision 2 created one but claimed it "cannot drift" from an unbounded range, which is not implementable and had already drifted (3.14 was missing). This row now names a mechanism that exists. It is also the evidence R7's dormancy decision consumes. R4, OQ3. |
| **Supply-chain scanning actually runs** | The declared security scanner executes on every change rather than sitting in the dev dependency list unused. | `bandit -r async_gateway -ll` as a named required CI step (R1). A declared control that never fires is the C8/H18/H19 pattern; if it is not going to run, it is removed instead. |
| **Accessibility / responsive design** | **Not applicable** — this is a library with no user interface. Noted rather than silently skipped. | — |

---

## Spec traceability: requirement → implementation approach → files

*(Distinct from, and additional to, the finding → requirement table in Part A. 35 rows, one per
requirement.)*

| Req | Implementation approach | Files |
|---|---|---|
| **R1** CI | A GitHub Actions workflow with a build job (`python -m build`), two independent clean-venv install-and-import-a-submodule jobs (wheel and sdist), lint / mypy / pytest / **bandit** / twine steps as required checks, an **interpreter matrix over the declared `requires-python` range** — the committed list `3.10`–**`3.14`**, with a test asserting it is ≥ the floor and contiguous and a scheduled job that fails when a newer stable minor exists (no test can derive a finite matrix from an unbounded range, so the earlier "cannot drift" claim is replaced by these two checks) — a **shuffled-order job** (`pytest -p randomly` × 5 committed seeds), and a **coverage-ratchet check** (head `fail_under` ≥ base `fail_under`). | `.github/workflows/ci.yml` (NEW), `tests/test_packaging.py` (matrix floor/contiguity test) |
| **R2** Test tooling | Add `pytest-asyncio` (`asyncio_mode=auto`) and **`pytest-randomly`**; build the HTTP/SOAP fixture as a **loopback `aiohttp.web` server via `aiohttp.test_utils.TestServer` with a request-recording handler** — **no aiohttp-mocking library**, because `aioresponses 0.7.9` (terminal release) is broken against all of aiohttp 3.14.x while resolving cleanly against it; pin `setuptools<81` or drop `flake8-import-order`; delete the tautological test and the dead coverage script; drop `-k pre_test`; enable `--cov=async_gateway --cov-branch` with the ratchet's initial `fail_under`; **exercise the fixture against the target aiohttp on day one**. | `tests/conftest.py` (NEW — the fixture), `pyproject.toml` (NEW), `tests/test_pass.py` (DEL), `tests/coverage_output.py` (DEL), `setup.cfg` (DEL) |
| **R3** orjson migration | Replace **all four** `ujson` sites — three direct call sites across three modules (`response_helper.py:14`, `filters_helper.py:45`, `:78`) plus the `json_serialize` wiring at `logic/http.py:30`; decode `orjson.dumps()`'s `bytes` at each direct call; wrap the default `json_serialize` to return `str`; reject a `bytes`-returning caller serializer at the boundary; drop `requests`. | `helpers/internal/filters_helper.py`, `helpers/internal/response_helper.py`, `logic/http_client.py` (**still named `logic/http.py` when R3 lands at Step 4 — Step 4.5 performs the rename**), `pyproject.toml` |
| **R4** Packaging | PEP 621 `[project]` + PEP 517 `[build-system]`; sdist file set; `requires-python`; truthful classifiers; `py.typed` added last. | `pyproject.toml` (NEW), `MANIFEST.in` (NEW), `async_gateway/py.typed` (NEW), `setup.py` (DEL), `setup.cfg` (DEL), `requirements*.txt` (DEL) |
| **R5** Version + provenance | Single version source in `pyproject.toml`, re-exported as `__version__`; drop `download_url`; annotated `v1.0.0` tag; a packaging test asserting metadata and `__version__` agree. | `pyproject.toml`, `async_gateway/__init__.py`, `docs/source/conf.py` (DEL), `tests/test_packaging.py` (NEW) |
| **R6** Dependency upgrades | Move to the pre-approved versions as ranges **at Step 1.5, in Phase 0, before any test exists** (Ruling B — this closes C7 there instead of at Step 21, which is now vacated). **No API migration step:** the three aiohttp surfaces (`ssl=`, `TraceConfig`, timeout API) and the S3 client are *rewritten* against the upgraded set by R14/R23/R26/R25, once each. `pytz` and `requests` stay declared until R9 (Step 5) and R3 (Step 4) stop importing them. Gate is install + resolve + clean-venv submodule import + the R2 fixture round-trip, plus an advisory scan on the resolved set. | `requirements.txt` (Step 1.5), `pyproject.toml` (Step 2 transcribes the same set) |
| **R7** Resilience-library decision | **Seven** fitness behaviours as tests. **Behaviours 1–6** decide fitness and run against `pyfailsafe` on the Step-1.5 dep set **across every interpreter in the R1 matrix** (`3.10`–`3.14`), because dormancy bites at the ceiling and not the floor. **Behaviour 7 (clock/sleep injectability) is already executed and FAILED** — `circuit_breaker.py:139,142`, `failsafe.py:103` — so it is recorded as a known fact and its three-way proposal is **resolved by Ruling D as keep-with-named-cost**, with R24's Step-14 facade holding the seam instead of the dependency. ADR records the choice, the matrix evidence, the behaviour-7 grep, why keep beat replace and vendor, the named cost **verbatim** (the facade owns the whole breaker state machine + retry loop; only exception classification + backoff computation are delegated — `retry_policy.py`, 136 of 539 lines, ~40 non-trivial), the owner, the revisit trigger, and OQ12's answer; vendored alternative stays bounded at **≤ 250 lines**, which against that residual is a live comparator rather than a formality. | `helpers/internal/circuit_breaker_helper.py`, `tests/helpers/test_circuit_breaker.py` (NEW), `docs/decisions/NNNN-resilience-library.md` (NEW), `.github/workflows/ci.yml` (the matrix) |
| **R8** Response envelope | One `GatewayResponse` TypedDict and one builder used by the entry point and all four protocols; `ok`/`status_code` promoted; `api_response`/`tat` removed; redaction (headers, cookies, `auth`, URL userinfo + sensitive query params, key-name payload masking to depth 4 — **no opt-out**) applied at construction; **transport helpers return a typed `HttpResult`, never a response shape** (FI-7's HTTP half — `request_helper.py:93`); **remote-status failures populate body/headers/cookies/status before raising** (E11); invariants **E1–E11** as tests. | `utils/envelope.py` (NEW), `utils/status_map.py` (NEW), `utils/redaction.py` (NEW), `async_gateway.py`, **`helpers/internal/request_helper.py`**, `logic/*_client.py`, `tests/test_envelope.py` (NEW) |
| **R9** Timestamps + latency | UTC ISO-8601 `request_time` from stdlib `datetime.timezone.utc` — **UTC only; the caller-supplied display timezone and `request_time_local` are cut**, so `pytz` goes and no `zoneinfo`/`tzdata` replaces it (OQ11); `time.monotonic()` durations; `get_ist_now` renamed; `start_time: float`. | `helpers/common/date_helper.py`, `utils/constants.py`, `helpers/internal/base.py`, `utils/envelope.py`, `pyproject.toml` |
| **R10** Error model + logger | Typed hierarchy with `super().__init__`, class-level `.code`, mandatory `raise ... from`; `unwrap_cause` with a depth bound; one conversion point in `request()`; `NullHandler` + per-module loggers with redacted structured `extra`. | `utils/exceptions.py` (REWRITE), `async_gateway/__init__.py`, `async_gateway.py`, all `logic/*_client.py` and `helpers/internal/*`, `tests/test_exceptions.py` (NEW) |
| **R11** Dispatch | Normalise once, typed registry, URL-scheme enforcement after pre-processors, `protocol_info` guard used consistently, SOAP registered. | `async_gateway.py`, `logic/__init__.py`, `helpers/internal/base.py`, `tests/test_entrypoint.py` (NEW) |
| **R12** Request construction | Shared media-type matcher (parameters stripped, case-insensitive) used by both request and response sides; **an explicitly-named filter table including a new raw-body branch for `text/xml` / `application/soap+xml` / `application/xml` and a named default that is no longer "the JSON filter for everything unknown"**; filter functions take a typed parameter set instead of `kwargs['request_type']` (today a hard `KeyError` for any caller without one — i.e. every SOAP call); case-insensitive verb comparison; `isinstance`-based bool coercion on a **copy**; documented defaults for download keys; dead branch decided explicitly. | `helpers/internal/filters_helper.py`, `helpers/internal/__init__.py`, `helpers/internal/request_helper.py` |
| **R13** Response handling | Distinguish malformed from empty; always set `text` alongside a decode error; binary-mode multipart writes; read parts to completion; buffered (non-quadratic) accumulation; handle a `None` part. | `helpers/internal/response_helper.py`, `helpers/internal/request_helper.py`, `logic/http_client.py` |
| **R14** HTTP transport | Optional caller-supplied session (not closed by the library); explicit `timeout=` on every session; **`max_response_bytes` default 64 MiB** enforced pre- and mid-read on every read path (the in-memory body, the streamed download, and SOAP's read — but not `fetch_file`, which R25 deletes); per-attempt body **factory**; 64 KiB chunk default; **the library owns the redirect loop (`allow_redirects=False` on the transport call) so R21's `allowed_schemes` can be re-checked on every hop before it is followed** (Ruling 4 / FI-16). | `logic/http_client.py`, `helpers/internal/request_helper.py`, `utils/http_file_config.py`, `utils/constants.py` |
| **R15** FTP | Initialise `verify_ssl` from `self.verify_ssl`; move the SSL block inside `try`; default FTPS on; fail closed rather than downgrade to `None`; conditional post-op `stat`; transfer timeout; verb allowlist; return the envelope. | `logic/ftp_client.py`, `helpers/internal/filters_helper.py` |
| **R16** SFTP transport security | Omit `known_hosts` (system resolution); expose `known_hosts`/`host_key` for pinning; `insecure_skip_host_key_check` opt-out with a warning; `client_keys=[]` default plus explicit key auth. | `logic/sftp_client.py` |
| **R17** SFTP operations | Initialise `remote_files` before the branch; delete the dead `self.remote_files`; consume typed `SFTPAttrs`; copy `additional_arguments`; connect/login timeouts; verb allowlist; return the envelope; fix the class docstring. | `logic/sftp_client.py` |
| **R18** SOAP client | `SoapRequest` subclassing `BaseRequestClass`, delegating transport to the existing HTTP helpers **and to R12's raw-body filter, so the envelope reaches the wire byte-identical to `build_envelope`'s output** (never through the JSON filter, which would also `KeyError` on the missing `request_type`); `build_envelope` (version-correct namespace, no double-wrap); `soap_transport_headers` (1.1 header vs 1.2 content-type parameter); **`soap_body` defined as the first element child of `<Body>`, `None` when absent/empty, first-with-a-warning when multiple**. | `logic/soap_client.py` (NEW), `logic/__init__.py`, `helpers/internal/__init__.py`, `helpers/internal/filters_helper.py`, `helpers/internal/request_helper.py` |
| **R19** SOAP Faults + XML hardening | **Prolog-scoped** `DOCTYPE` rejection (a `<!DOCTYPE` before the root element — *not* a body-contains search, which false-positives on a Fault `<detail>` echoing HTML), then stdlib `ElementTree`; **the read goes through R14's capped reader, which R19's security argument depends on and now declares**; Fault detection for both versions independent of transport status; structured `SoapFault` into `protocol_details`; `SerializationError` on non-envelope XML. | `logic/soap_client.py`, `helpers/internal/request_helper.py` (the cap), `utils/exceptions.py`, `utils/status_map.py` |
| **R20** No blocking I/O | Convert all four sites to `aiofiles` / `aiofiles.os`; add an AST-based test asserting no bare `open(` inside any `async def`; **loop-fairness test stated as a counting assertion (≥ 1 `sleep(0)` round-trip per downloaded chunk over 256 chunks), not a wall-clock bound** — deterministic, and compatible with R28's no-sleep rule. | `helpers/internal/request_helper.py`, `utils/http_file_config.py`, `tests/test_no_blocking_io.py` (NEW) |
| **R21** Verb allowlists + URL trust | Per-protocol `Enum`/frozen allowlists consulted before every `getattr`; `allowed_schemes` default `{http, https}`, **enforced on every redirect target and not only the initial URL** (Ruling 4, via R14's owned loop — a rejected hop raises before the next request is issued); README section on caller-owned URL validation; a test asserting allowlists and README agree. | `helpers/internal/base.py`, `logic/*_client.py`, `helpers/internal/request_helper.py`, `utils/http_file_config.py`, `README.md` |
| **R22** Path containment | `resolve_within` (canonicalise + containment) before every write; `safe_write` with `O_NOFOLLOW`/`O_EXCL` and a restrictive mode; idempotent `safe_unlink`; `try/finally` cleanup of partials. | `utils/paths.py` (NEW), `utils/http_file_config.py`, `helpers/internal/request_helper.py`, `logic/ftp_client.py`, `logic/sftp_client.py` |
| **R23** TLS + client certs | `Purpose.SERVER_AUTH`, assert `check_hostname` and `CERT_REQUIRED`, return under `ssl=`; replace `verify_ssl or True` with an explicit decision; a loopback mTLS handshake test. | `helpers/internal/filters_helper.py`, `tests/helpers/test_filters_helper.py` (NEW) |
| **R24** Circuit breaker | Move construction out of `__init__` into a bounded LRU registry keyed `(family, host, port)`; typed config rejecting unknown keys; **an explicit `clock` / `sleep` seam on `CircuitBreakerConfig` and `get_breaker`, defaulting to `time.monotonic` / `asyncio.sleep`, which every timing assertion in the spec drives** — and, per Ruling D, **implemented in this library's own breaker facade** (which owns the whole breaker state machine and the whole retry loop, delegating only exception classification and the backoff computation) rather than expected from `pyfailsafe`, which provably has no such seam; the facade's interface is identical under R7's keep / replace / vendor, so Step 14 does not wait on Step 22; reachable `0`; `isinstance` accepting float and rejecting bool; named backoff; non-zero delays and `jitter=True`; copy the caller's config. | `helpers/internal/breaker_registry.py` (NEW), `helpers/internal/circuit_breaker_helper.py`, `helpers/internal/base.py`, `utils/constants.py` |
| **R25** File utilities | Delete the duplicate module and `fetch_file`; single S3 downloader on the current aioboto3 API with keyword-only parameters; refuse to write on any non-success status; drop `STATUS_CODE_403`. | `helpers/common/file_helper.py` (DEL), `helpers/internal/request_helper.py`, `utils/http_file_config.py`, `utils/constants.py` |
| **R26** Request tracer | Store results on the per-request trace `context` and collect at the end; relative delta for `reuseconn`; string (not exception) for the exception message; full annotations on all **15** registered callbacks (`utils/request_tracer.py:111-125` — the count is 15, not the 14 the audit's prose said). | `utils/request_tracer.py`, `logic/http_client.py`, `utils/envelope.py` |
| **R27** Lint + types | flake8 runnable from a clean install and at zero violations; legal `application_import_names`; `A005` resolved by **three renames performed at Step 4.5 in Phase 0** (plus `soap_client.py`, created under its final name — not a fourth rename), so that every test path and traceability row in this plan is written once, in post-rename names (FI-15); `ignore_errors` removed; suppression-justification CI check; formatter in check mode. | `pyproject.toml`, `.flake8`, `logic/http.py→http_client.py`, `logic/ftp.py→ftp_client.py`, `logic/sftp.py→sftp_client.py` (all three at Step 4.5), `.github/workflows/ci.yml` |
| **R28** Coverage | `--cov=async_gateway --cov-branch` **from Step 1**, with `fail_under` **ratcheting monotonically** from the then-measured floor to exactly **100 at Step 26** and a CI check failing any commit that lowers it; boundary-specific mocks; the injected clock/sleep seam for every timing assertion; parametrised tables for the repetitive matrices; pragma-justification and **pragma-ceiling-of-10** CI checks; `pytest -p randomly` over five committed seeds; **outbound**-network-disabled run with `127.0.0.1` allowlisted (two fixtures bind loopback: the `aiohttp.web` HTTP/SOAP server and R23's TLS server). | `pyproject.toml`, `tests/conftest.py` (NEW), the whole `tests/` tree, `.github/workflows/ci.yml` |
| **R29** README | Full rewrite around the consumer; the R8 envelope and R10 error table documented as the contract; mechanical tests that compile every `python` block, import every documented path, check every documented signature, and assert the disclosures are present — the payload-echo disclosure (the mechanical home for R8's "the README says so plainly" clause) **and the three SOAP consumer facts: the request body is a hand-built XML string or `Element` with no dict-to-XML mapping, `soap_body` is a raw `Element` and not a mapping, and MTOM/`multipart/related` is unsupported** (the mechanical home for R18's "documented as out of scope" claim); no corporate endpoints. | `README.md` (REWRITE), `tests/test_docs.py` (NEW) |
| **R30** Source docs + typing | Module docstrings with content everywhere; docstrings on every public symbol; `disallow_untyped_defs`/`disallow_incomplete_defs`; named typed structures instead of bare `Dict`/`List`; the six specific documentation defects fixed. | every `.py` under `async_gateway/`, `pyproject.toml`, `tests/test_docs.py` |
| **R31** Sphinx retirement | Delete the three Sphinx files and the two dev dependencies (recommended), or add a root `.rst` and a working POSIX build verified in CI (alternative). Decision recorded in the CHANGELOG. | `docs/make.bat` (DEL), `docs/source/Makefile` (DEL), `docs/source/conf.py` (DEL), `pyproject.toml`, `CHANGELOG.md` |
| **R32** Agent instructions | Replace the FastAPI command block with this project's real commands and the "adding a protocol" recipe. **Blocked on OQ8 approval.** | `CLAUDE.md` (MODIFIED — user approval required), `.claude/rules/fastapi-patterns.md` (raised in the same ask) |
| **R33** CHANGELOG | Keep-a-Changelog `1.0.0` entry naming the version reset and every breaking change; records the R7 and R31 decisions; CI step requiring a changelog touch alongside `async_gateway/` changes. | `CHANGELOG.md` (NEW), `.github/workflows/ci.yml` |
| **R34** LICENSE | Verify the MIT text byte-for-byte against a committed reference; the copyright line is a **human decision** (OQ7); metadata, classifier and file made to agree. | `LICENSE`, `pyproject.toml`, `tests/test_packaging.py` |
| **R35** Examples | Five ≤40-line runnable scripts, one per protocol plus error handling; compile-checked in CI; no third-party endpoints; packaging inclusion decided and asserted. | `examples/*.py` (NEW), `.github/workflows/ci.yml`, `tests/test_packaging.py` |

---

## Self-critique (RARV · Reflect)

Written before handoff, per `.claude/rules/rarv-cycle.md`. Each item is either resolved above or
recorded as an Open Question.

**The weakest requirement — most likely to be wrong or to change: R28 (100% line + branch coverage).**
It is locked by human decision, but its cost is concentrated in a place nobody has measured: driving
`asyncssh` and `aioftp` to full *branch* coverage through mocks (assumption **A6**). If those clients
cannot be fully driven without a live server, R28 stops being a testing task and becomes a
containerised-fixture infrastructure task — the single biggest schedule risk in this plan. *Resolved
as far as possible:* A6 names it, the testing strategy names the specific branch shapes that leak, and
OQ10 asks whether the gate stays on after `1.0.0` rather than letting it erode silently.

**The riskiest assumption: A1 — the package is still unpublished.** Every "breaking change is free"
argument here rests on it: the version reset (R5), the envelope replacement (R8), the removal of
`api_response`, the module renames (R27). If anything is uploaded before this lands, three requirements
change shape at once and the version can only go forward. *Resolved:* OQ4 makes re-verifying the PyPI
404 a gating step immediately before R5, recorded in the ticket work log.

**And the assumption this self-critique got wrong — recorded rather than quietly corrected.** Two
revisions of this document nominated A1 as the riskiest assumption and A6 as the biggest schedule
risk. Both were plausible and both were **wrong about which assumption would actually break the plan.**
It was **A5** — "`aioresponses` can mock the aiohttp R6 selects" — listed with the right fallback,
never executed, and false. A1 was executed and held (PyPI still 404s). The lesson generalises past
this one line, so it is written into the plan as a **standing constraint** (see Assumptions) and given
a mechanical home in R2/Step 1 rather than left as a reflection: *this spec's assumptions were ranked
by consequence-if-false and not by how-cheap-to-check, and the cheap-to-check one was the one that
was false.* The two EM passes could not have caught it — both said plainly they had no execution tool,
and a read-only reviewer cannot close an execution-shaped assumption. That is a **process** finding as
much as a spec one: when a gate's open assumptions are all of that shape, the reviewer needs an
execution tool or the assumptions must be routed to someone who has one.

**An acceptance criterion that was not truly testable, and what I did about it.** R29's original
instinct was "the README is rewritten for consumers" — unfalsifiable. It is now five mechanical tests
(compile every `python` block, import every documented path, check every documented signature, assert
the `protocol_info` keys in the code and in the README are the same set, and assert the payload-echo
disclosure is present) plus a `grep -c` on `api.fyndx1.de` returning 0. Similarly R30's "documentation is improved" became
`disallow_untyped_defs` plus a test that a module docstring is **≥ 40 characters and ≥ 6 words** and is
not merely the filename — which is what makes `"""Ftp."""` fail.

**The criteria that are *not* mechanical — the complete list.** An earlier draft claimed only R7's was
human-judged, which over-counted this spec's own rigour. There are **six**, and each is discharged by
requiring a named artifact a reviewer can open, not by a command. R8's redaction criterion is
deliberately **not** among them: its "and the README says so plainly" clause (echoed at E9 and in the
NFR secret-hygiene row) reads as human-judged, so R29 gives it a mechanical home — a doc test
asserting the README carries the payload-echo disclosure — rather than growing this list to seven:

| # | Criterion | Where | How it is discharged |
|---|---|---|---|
| 1 | "The trade-offs are written down" | R7 | A named ADR under `docs/decisions/` with named sections (option chosen, evidence, options rejected, accepted cost, revisit trigger). |
| 2 | "A deliberate CI regression is recorded once as evidence" | R1, crit. "A deliberate regression test of CI itself" | An entry in the ticket work log with the failing job's URL. Deliberately not a permanent test. |
| 3 | "A deliberately failing test made `pytest` exit non-zero" | R27, crit. 1 | Same: work-log evidence from a scratch branch, not a committed test. |
| 4 | "The classifier is chosen **deliberately** and justified" | R4 | A sentence in the CHANGELOG naming which `Development Status` was chosen and why. |
| 5 | "The R31 docs decision is recorded" | R31 | A CHANGELOG entry stating delete-vs-keep and the reason. |
| 6 | "The copyright line is confirmed correct **by the human**" | R34 (OQ7) | A recorded human decision; an agent cannot make it. |

Two of these (2 and 3) are *evidence* criteria rather than tests **on purpose**: a permanent test that
CI can fail is a test that fails CI. Naming them here means the code reviewer expects a work-log link
and does not go looking for a test file.

**And the thresholds that used to be deferred are now numbers.** Four criteria previously said "a
stated minimum" / "a committed ceiling" / "a documented default" and named no value, which meant the
number would be chosen at implementation time — i.e. under schedule pressure, by whoever was closest to
missing it. They are now fixed in the spec: the pragma ceiling is **10**, the module-docstring minimum
is **40 characters and 6 words**, `max_response_bytes` defaults to **64 MiB**, and R20's
loop-starvation bound is **≥ 1 event-loop round-trip per downloaded chunk over 256 chunks** (a counting
assertion, not a wall-clock one — which also removes its conflict with R28's no-sleep rule). Each
carries its justification at the point of use.

**The one thing most likely to go wrong in implementation: the gates going on all at once.** The
audit's premortem already caught this and reordered its own top-10 around it, and the earliest signal
it names — `ignore_errors = True` restored within a week — is a *process* failure, not a code one. This
spec pushes back with sequencing (Phase 0 before everything, Phase 6 one gate per commit, R30 before
R27's docstring rules, `py.typed` last) and with FI-8/FI-13, but sequencing written in a document is
weaker than sequencing enforced by a tool. **The residual risk is real and is not fully mitigated
here.** The strongest available mitigation is that R1's CI lands in Step 3, so every subsequent step
has a bisectable green baseline — which is the whole reason scaffolding comes first.

**A second thing I argued myself into and want challenged: the `MG1`–`MG8` series (OQ1).** I found eight
Medium findings the verified inventory did not cover, and I chose a parallel id series to avoid
contradicting a ground truth that the main session will grep. That preserves the grep *and* the
findings, but it leaves the project with two Medium id schemes, which is a small permanent tax on
anyone reading the report and the spec together. The alternative — renumbering to `M29`–`M36` and
correcting the ground truth — is cleaner but breaks the mechanical check this spec is about to be
verified by. I picked the reversible option and flagged it; the EM should overturn it if the
mechanical check can be updated. **Adjudicated in EM review iteration 1: `MG1`–`MG8` stand.** The EM
counted exactly eight clauses at `repo-audit-report.md:638-648`, verified each against source, and
endorsed the `MG` series over `M29`–`M36` on the grounds that the audit report stays in the repository
as the traceability anchor and renumbering would put the spec and a committed report into permanent
disagreement. `L1`–`L17` were verified and endorsed on the same pass (16 middle-dot clauses with the
`circuit_breaker_helper` clause correctly split in two). OQ1 is therefore answered in the affirmative;
it remains listed only because the *human* has not yet ratified it.

**What this plan gets right, and what it costs (balance sheet).** It gets right that the contract
(R8/R10) has to land before the protocol work, that the gates have to go on last and one at a time, and
that sixteen fix-interactions bundle or order work that a naive story split would separate. What it costs: 35
requirements over ~1,133 LOC is a heavy plan for a small library, the SOAP implementation is genuinely
new code in a release otherwise about remediation, and R28's 100% bar plus R33's changelog gate are the
two things most likely to be resented and quietly disabled six months from now. Both carry cut criteria
and OQ10 asks the question directly rather than assuming the answer.

---

## Revision history

### Revision 4 — 2026-08-16 · implementation-phase rulings on the logging contract (OQ13, OQ14)

Two amendments made **during Phase 2**, both ratified by the human main session before S8 was
dispatched. Neither changes a requirement id, a step, or the acceptance-criterion count (still 258).

| Ruling | Change | Where |
|---|---|---|
| **OQ13 RATIFIED — Ruling I stands. The redacted traceback stays; `exc_info=True` does not return.** Rationale of record: the shared redactor is a spec deliverable *precisely because* the audit proved secrets reach logs. `exc_info=True` hands the **live chained exception** to arbitrary application handlers, where no redactor of ours can run — a bare `logging.basicConfig()` prints the raw aiohttp `str()`, secret included. `raise … from None` was implemented and empirically **rejected**: `traceback.format_exception` hides the text but `__context__` survives on the object, so chain-walking APMs (Sentry et al.) still read it. Only removal is unconditionally safe. **Accepted cost:** loses ecosystem-standard exception grouping/fingerprinting; `extra['traceback']` is invisible to most handlers. *Owner:* the maintainer. *Revisit trigger:* a redaction-aware handler ships, or a consumer reports APM grouping loss. *Reversal:* one line in `log_failure` + two guard tests — a two-way door. **S29 must document `extra['traceback']`, the loss of native APM exception grouping, and the one-line revert as a documented consumer choice.** The two guard tests stay. | §Logging contract · R9's logger acceptance criterion (:833-838) · the `async_gateway.py` conversion-point sketch · the six `error` + `exc_info` rows of the failure table |
| **OQ14 RESOLVED — the single-report principle.** The failure table marked the four pre-dispatch `(raises)` rows `Logged: error`, contradicting "every failure logs exactly once, **at the conversion point**" — a path that raises before dispatch never reaches that point. Resolved in favour of the conversion-point rule: **an error is reported exactly once.** Either it escapes to the caller as a `raise` (the four `(raises)` rows — these do **not** log; the caller holds the exception object, and reporting it twice is the defect) or it is converted to an envelope inside `handle_request` and logged at that conversion point. There is no third behaviour. If an implementation surfaces a concrete reason this reading is wrong, the story **stops and reports** rather than improvising one. | The four `(raises)` rows' **Logged** column → `—` (:3815-3818) · a new single-report bullet in §Logging contract |

### Revision 3 — against the Devil's Advocate plan critique (`.claude/state/gate-evidence/devils-advocate-plan-critique.md`)

Verdict addressed: **UPHELD — C1 · M2 · L1**, plus the orchestrator's Rulings A–G. What changed:

| Ruling / finding | Change | Now lives in |
|---|---|---|
| **A** — DA Finding 1 (Critical): `aioresponses 0.7.9` is broken against all of aiohttp 3.14.x, and the plan's ordering detonates it at Step 21 of 31 | **`aioresponses` is dropped entirely.** The primary HTTP/SOAP fixture is a loopback `aiohttp.web` server via `aiohttp.test_utils.TestServer` with a request-recording handler. Removed from R2, Step 1, the dev dependency set and the traceability rows; **no** aiohttp-mocking library is added, and pinning one against an older aiohttp is explicitly rejected. The four criteria written in aioresponses' vocabulary are rewritten in the fixture's. Two consequences that would otherwise have been orphaned are also fixed: R28's "no test touches the network" (loopback is now allowlisted, since two fixtures bind it) and R14's timeout test (a handler awaiting an `asyncio.Event`, not a mock-injected exception — and why that is not the sleep-to-settle pattern R28 forbids). | R2 · R3 · R14 · R18 · R21 · R24 · R28 · Dependencies §Development · Testing strategy · Steps 1/4 · traceability R2/R3/R28/R29 |
| **B** — the ordering, not just the tooling | **R6/C7 moves to Step 1.5, in Phase 0**, so the test architecture is built once against the transport it ships on. R14/R23/R25/R26 *rewrite* the affected surfaces against the upgraded set — no migration commit, no re-run gate. **Step 21 is vacated**, not renumbered. `pytz` and `requests` stay declared until R9 (Step 5) and R3 (Step 4) stop importing them, which is the one part of R6 that cannot move. | R6 · FI-10 · FI-11 · FI-12 · Step 1.5 (NEW) · Steps 2/3/4/5 · Step 21 (vacated) · Step 22 · requirement-ordering table · C7 traceability row · "Said out loud" (rewritten) |
| **B** — the C7 disclosure describes a trade the plan no longer makes | The "a Critical stays open for most of the delivery window" section is **withdrawn and replaced**, not softened; the Revision-2 justification at its core is recorded as having been weaker than the risk it created, with the executed evidence that overturned it. | "Said out loud: C7 now closes in Phase 0" |
| **C** — A5 stops being an assumption | A5 becomes a **decided fact** carrying the executed bisection across six aiohttp versions and the verified replacement. Generalised into a **standing constraint** — "tool X supports version Y" is closed only by executing a call through X against Y — with a mechanical home: a Step 1 criterion driving the fixture against the *target* aiohttp on day one. The "live-verified 2026-08-15" claim is re-scoped to what a resolver check actually establishes. | Assumptions §decided fact · §standing constraint · A5 row · R2 · Step 1 · Dependencies §Runtime |
| **D** — DA Finding 2 (Medium): behaviour 7 is provably false, so Step 22's proposal is certain | Behaviour 7 is recorded as **known-failed at planning time** with the three grepped lines as evidence, and its three-way proposal is **resolved now**: keep `pyfailsafe` with a named cost, and **R24's own breaker facade holds the `clock`/`sleep` seam**. The cost is stated precisely (the facade owns the whole breaker state machine and the whole retry loop, including failure counting and the callback invocations; the dependency keeps exception classification and backoff computation only — `retry_policy.py`, 136 of 539 lines, ~40 non-trivial), with owner and revisit trigger. Step 14 no longer waits on Step 22, so Step 26's terminal coverage gate is off an unestimated decision. Flagged as overturnable: **OQ12**. | R7 · R24 · A8 · `CircuitBreakerConfig` §seam · `get_breaker` · Steps 14/22 · OQ12 (NEW) · traceability R7/R24 · file-structure row |
| **E** — DA Finding 3 (Medium): the matrix ceiling is stale where the spec says the ceiling is load-bearing | **3.14 added** to the matrix. The unsupported "the matrix cannot drift" claim is replaced with a mechanism that exists: a committed list, a test that it is ≥ the floor and contiguous, and a scheduled job that fails when a newer stable minor appears. Bounding `requires-python` instead is raised as the alternative. | R1 · OQ3 · NFR portability row · Step 3 · R7 crit. 2 · traceability R1 |
| **F** — DA Finding 4 (Low): R29 under-specifies what a SOAP consumer hits first | R29's SOAP criterion gains all three: the request body is a hand-built XML string or `Element` (no dict-to-XML), `soap_body` is a raw `Element` and not a mapping, and MTOM is unsupported — each asserted by `tests/test_docs.py`, so R18's "documented as out of scope" claim finally has an owner and a test. | R29 · R18 edge cases · traceability R29 |
| **G** — the half-applied L-g fix | Every **call-site** count now says **four**; every **module** count stays **three**. Both numbers are named explicitly where they appear so they cannot be conflated again. | R3 crit. 4 · Step 4 · traceability R3 |

**Not changed, deliberately:** all 89 finding ids (C×7 with C5 vacated, H×29, M×28, MG×8, L×17) and all
35 requirements are preserved, and no step is renumbered — Step 1.5 is inserted and Step 21 is vacated,
following the same "empty the slot, never reuse it" rule this spec already applies to `C5`.

### Revision 2 — against EM review iteration 1 (`.claude/state/gate-evidence/em-review-iter1.md`)

Verdict addressed: **C0 · H2 · M12 · L10 · Cos5**. What changed, by finding:

| Finding | Change | Now lives in |
|---|---|---|
| **EM-H1** FI-10 contradicted Step 21 | FI-10 rewritten as an **ordering + re-run gate** (R23/R26 land at Steps 15/19; R6 re-runs their test sets as its own gate) rather than a one-commit bundle. R23's edge case and R6's criteria updated to match; the one-commit reading is deleted. | FI-10 · R6 crit. "The R6 story's own gate" · R23 edge cases · Step 21 |
| **EM-H2** redirect-target scheme re-check | **Enforced in 1.0.0.** The library owns the redirect loop (`allow_redirects=False`), re-checks every resolved `Location` against `allowed_schemes` before following, and refuses rather than observes. Moved out of the M21 deferral and out of the out-of-scope list. | R14 redirect criterion · R21 new criterion · FI-16 · Step 13 · Deferral summary |
| **EM-M1** SOAP body → JSON filter / `KeyError` | R12 now names the filter table explicitly, adds a **raw-body branch** for `text/xml` / `application/soap+xml` / `application/xml`, replaces the `'default' → JSON` entry, and requires typed filter parameters instead of `kwargs['request_type']`. R18 asserts byte-identical wire body. | R12 crits. 2–3 · R18 crit. "byte-identical" |
| **EM-M2** DOCTYPE guard false-positives | Guard is now **prolog-scoped** (prolog scan or `StartDoctypeDeclHandler`), with two required tests: billion-laughs rejected, in-element `DOCTYPE` accepted. Substring matching explicitly forbidden. | R19 crit. "DOCTYPE rejection" · `parse_soap_response` |
| **EM-M3** `soap_body` undefined | Defined as the **first element child of `<Body>`**; `None` when absent/empty; first-with-a-warning when multiple. Consistent with the HTTP-202 criterion, which the `<Body>`-element reading contradicted. | R18 · `protocol_details` table · `parse_soap_response` |
| **EM-M4** FI-7's HTTP half unowned | R8 gains the `grep` criterion, `request_helper.py` joins R8's file list, `HttpResult` is specified, and **Step 5** performs the deletion of `request_helper.py:93`. | R8 crit. "Transport helpers return data" · `HttpResult` interface · Step 5 · FI-7 |
| **EM-M5** rename lands too late | Rename moved to **Step 4.5 (Phase 0)**; Step 24 becomes a verification; R27's criterion corrected to three renames plus one new file; OQ9's deadline moves to before implementation. | Step 4.5 · FI-15 · R27 A005 crit. · OQ9 |
| **EM-M6** coverage ratchet not mechanical | `--cov-branch` from Step 1 with `fail_under` at the measured floor; every step raises it; CI fails any commit that lowers it; terminal exactly 100 at Step 26. Mirrored into the step-list preamble. | R28 crits. 2–3 · R1 ratchet step · Steps 1/3/26 preamble |
| **EM-M7** no clock seam | `clock` / `sleep` added to `CircuitBreakerConfig` and `get_breaker`; R28 states every timing assertion drives it; the testing-strategy clock row names the seam. | `CircuitBreakerConfig` · `get_breaker` · R24 crit. 4 · R28 edge cases · Step 14 |
| **EM-M8** `pytest-randomly` undeclared | Declared in R2 and in the dev dependency list; R28's criterion restated as a runnable command; R1 gains a shuffled-order job over five seeds. | R2 · R28 · R1 · Dependencies |
| **EM-M9** R14 cap undeclared as R19's dependency | R19 gains a capped-read criterion; R18's reuse criterion names the cap; a **requirement-ordering table** now records this and seven other hard inter-requirement dependencies. | R19 · R18 · Dependencies §"Requirement ordering" |
| **EM-M10** error statuses drop the body | New invariant **E11** plus an R8 criterion and a named 404-with-JSON-body test; the error-handling section shows the required populate-then-raise sequence; `finalise_error` documented as additive only. | E11 · R8 · Error-handling design · edge-case map |
| **EM-M11** E9 broader than the redactor | `redact_url` and `redact_payload` specified (userinfo, sensitive query-parameter names, key-name masking to depth 4); **E9 rewritten to claim exactly what the redactor delivers**; the README states the payload is echoed verbatim below depth 4; the logging contract uses `redact_url`. | `utils/redaction.py` · E9 · R8 redaction crit. · Logging contract |
| **EM-M12** CI matrix claimed, never created | R1 gains a matrix criterion over the declared range with a metadata-agreement test; R7's fitness criterion moves from "the floor" to "every interpreter in the range". | R1 · R7 crit. 2 · NFR portability row · OQ3 |

Also applied: the **C7 disclosure** (a Critical open until Step 21, stated with its trade), the
vendored-breaker bound raised **150 → 250** with the estimate treated as a live input, a **rollback and
reversal** section, all ten Low and five Cosmetic items, and the corrected counts (**15** tracer
callbacks, **four** `ujson` references). Three sub-features were adjudicated: the display timezone and
`redact=False` are **cut**; the redirect controls are **kept, narrowed** to EM-H2's vehicle.
