# Story Breakdown: async-gateway v1.0.0 — Remediation and First Publish

**Source spec:** `docs/specs/v1_release_spec.md` (4,152 lines · R1–R35 · **258** acceptance criteria ·
89 findings), gated `spec-complete` + `em-approved`
(`.claude/state/gate-evidence/spec-complete.md` — DA round 3 **CONFIRMED-WITH-COSTS**).
**Stage:** 1f — Story Breakdown & Coverage Gate.
**Result:** coverage gate **PASS** — 258/258 criteria mapped · **0 gaps** · **0 scope creep** ·
graph **acyclic** · 32 stories.

> The spec is the contract. Nothing here re-opens it. Where this breakdown could be read as
> disagreeing with the spec's 31-step sequence, **the sequence wins** and the deferral is recorded in §11.

---

## 1. Decomposition approach

The spec already decomposes the work: *"The release lands as a sequence of commits on a branch, each a
single step from the implementation-steps list, each independently green"* (§Rollback and reversal).
That step list is the output of an adversarial process that overturned two earlier orderings, and
every FI-1…FI-16 constraint is already discharged by it.

**So: one story per implementation step.** `Step 1.5` and `Step 4.5` are insertions; **`Step 21` is
vacated** and carries no story — retained as an empty row on the spec's own vacated-`C5` principle.
32 real steps → **32 stories**. No step is merged with another. A step is split only where its files
are disjoint *and* the split preserves the sequence — which happens **nowhere**, because every
multi-requirement step (5, 27, 30) shares files across its requirements (§8).

**Step** is the merge-order key throughout. **File boundary** is exhaustive: a path in no boundary is
out of scope for everyone.

---

## 2. Coverage-gate result

| Measure | Value |
|---|---|
| Acceptance criteria in the spec (`grep -c '^- \[ \] '` → 258, verified) | **258** |
| Criteria mapped to ≥ 1 story | **258** |
| **Gap count** (criterion with no story) | **0** |
| **Scope-creep count** (story with no criterion) | **0** |
| Requirements covered | **35 / 35** |
| Part B invariants E1–E11 bound to a story | **11 / 11** |
| Findings reachable through the mapped requirements | **89 / 89** |
| Stories | **32** · Waves **25** · max lane width **3** |

Two structural notes, carried rather than hidden:

- **R31 is an either/or pair** (AC1 delete the Sphinx files · AC2 keep an API-docs build). Both map to
  **S28**; one is discharged, the other recorded not-applicable with its reason. The spec's own
  construction, not a gap.
- **Nine criteria are *written* early and go **green** later** — by design (Step 7: *"They will fail
  for FTP/SFTP/SOAP; that is the point"*). §10 gives those a **green-at** column so a reviewer does
  not read a red test as an unmet criterion.

---

## 3. Three conventions the orchestrator must adopt before Wave 0

Without these the plan is **single-threaded**, because the spec's own machinery puts the same file in
every story's scope.

**C-1 · The ratchet protocol — `pyproject.toml :: fail_under` is integration-owned.**
R28 requires every step to end by raising `fail_under`, *"part of the step's own commit."* Taken
naively that puts `pyproject.toml` in all 32 boundaries and forbids concurrency — and it **breaks the
gate mechanically**: R1's ratchet job compares head against base, so if lane A raises 40→55 and
concurrent lane B (branched at 40) raises 40→50, merging B after A gives base 55 / head 50 and the job
fails. **Protocol:** lanes never touch `fail_under`; the integrator merges in step order and, per lane,
rebases → re-measures → **amends the raise into that lane's own commit** → lands. The raise stays in
the step's commit (R28 satisfied literally) and the ratchet stays monotone (R1 satisfied).
*Confirm this reading before Wave 0 — it is the one assumption the lane plan rests on.*

**C-2 · The fixture protocol — `tests/conftest.py` is integration-owned.**
Six stories add a test double. If they all edit `conftest.py`, Phases 2–3 collapse to one lane.
Each double lives in its own `tests/fixtures/<name>.py` owned by exactly one story; `conftest.py`
holds only imports/registrations and is written by the integrator at merge. S1 seeds both.

**C-3 · Merge order is always spec step order.**
Lanes may *develop* out of step order; they may never *merge* out of it. This is what lets Steps
14/15/16 run in three worktrees while branch history still reads 1 → 1.5 → 2 → … → 31, which the
spec's rollback story requires (*"reverting one step is `git revert` of that step's commit"*).

---

## 4. Story catalogue

Sizing: **S** ≤2 files · **M** 3–5 · **L** 6–8 · **XL** 9+. Paths under `async_gateway/` are given
relative to it. Four stories exceed M and cannot be split — §12.

### Phase 0 — Scaffolding

| Id | Step | Requirements / findings | File boundary | Definition of done (spec's own checks) | blockedBy |
|---|---|---|---|---|---|
| **S1** Suite can fail, runs async, is measured | 1 | R2 all · R27-AC1 · R28-AC1,2,3 · C8 M26 M27 H19 · FI-5 · **M** | +`tests/conftest.py`, `tests/fixtures/{__init__,http_server}.py`, `tests/test_fixture_smoke.py` · ~`setup.cfg`, `requirements-dev.txt` · −`tests/test_pass.py`, `tests/coverage_output.py` | `grep -rn "pre_test" .` → 0 outside spec/audit · fresh venv `flake8 --version` exits 0 · unmarked `async def` test runs · `pytest -p randomly --randomly-seed=1` exits 0 · a deliberately failing test on a scratch branch makes pytest exit non-zero, **recorded in the work log** (not committed) · `--cov=async_gateway --cov-branch` on, `fail_under` = then-measured floor · **no aiohttp-mocking library in the dev set** (Ruling A) · **FI-5:** `coverage_output.py` deleted in the same commit that enables `--cov-branch` | — |
| **S2** Upgrade the dependency set — closes C7 in Phase 0 | 1.5 | R6-AC1,3,4,5,6 · R3-AC2 · C7 H23 · FI-10/11/12 · **S** | ~`requirements.txt` **only** — no source file may change | Ranges with approved floors + major ceilings (`aiohttp>=3.14.3,<4`, `orjson>=3.12.0,<4`, `aioboto3>=15.5.0,<16`, `aiofiles>=25.1.0,<26`, `aioftp>=0.28.0,<1`, `asyncssh>=2.24.0,<3`); `grep -c "=="` over the table → 0 · **`pytz` and `requests` deliberately stay** (S7 and S5 own their removal — dropping either here reddens the clean-venv import) · **no aiohttp API migration** · fresh venv resolves with no backtracking; `python -c "from async_gateway.async_gateway import request"` succeeds (the FI-11 catch, cheapest moment) · S1's fixture round-trip re-runs against the now-declared aiohttp · advisory scan on the **resolved** set = **zero Critical/High** (absolute, never a delta against "38 in 3 packages") | S1 |
| **S3** Declarative PEP 621 packaging | 2 | R4-AC1,2,3,4,6,7,8 · M22 M23 H26 L16 L17 · **M** | +`pyproject.toml`, `MANIFEST.in`, `.flake8`(if needed) · −`setup.py`, `setup.cfg`, `requirements.txt`, `requirements-dev.txt` | `[build-system]`+`[project]` complete · **the dependency table transcribed is the S2 set** — written once, never as old pins a later step amends · `requires-python=">=3.10"` (OQ3) · S1's coverage config moves from `setup.cfg` at **the same value** (the move must not lower the ratchet) · `python -m build` → both artifacts; `twine check` passes; sdist contains README/LICENSE/pyproject; `pip install --no-binary :all:` succeeds in a clean venv · `unzip -l dist/*.whl` shows no `tests`/`docs`/`local_development` · **`py.typed` NOT added** (FI-13) · no `open()` without `encoding=` in the build path | S2 |
| **S4** CI | 3 | R1-AC1…9,11 · R28-AC7(job) · H21 · **S** | +`.github/workflows/ci.yml`, `tests/test_packaging.py` | build → clean-venv **wheel** install → submodule import → same again from **sdist** (`--no-binary :all:`), wheel cache disabled · named steps `flake8` (baseline file) · `mypy` (still `ignore_errors`) · `pytest` · `bandit -r async_gateway -ll` · `twine check` · **matrix `3.10–3.14` committed, not derived**, + a test that it is ≥ the floor and **contiguous**, + a **scheduled monthly job** failing when a newer stable minor exists · **shuffled-order job** over five committed seeds · **ratchet job** (head ≥ base `fail_under`) · CI stands up on the **S2 set** — never green on the un-upgraded one · work-log evidence: scratch branches re-adding `ujson`, lowering `fail_under`, and deleting `3.14` each turn a job red | S3 |
| **S5** Finish the `orjson` migration — **four** call sites | 4 | R3-AC1,3,4,5,6,7 · C1 MG2 · FI-6 · **M** | ~`helpers/internal/response_helper.py`, `helpers/internal/filters_helper.py`, `logic/http.py` *(pre-rename)*, `pyproject.toml` · +`tests/helpers/{__init__,test_filters_helper,test_response_helper}.py` | **See the call-site box below — the count is four, not three.** `grep -rn "ujson" async_gateway/` → 0 · `grep -rn "^import requests\|^from requests" async_gateway/` → 0 · a `bytes`-returning caller serializer is rejected at the boundary with `ConfigurationError`; `serialization=json.dumps` keeps working · the S4 clean-venv job — failing in reality while passing in CI's pre-installed env — now passes against a truly clean venv · `orjson.loads` on empty bytes raises a `ValueError` subclass, and the handler catches the `ValueError` **base** so the migration does not change which type escapes | S4 |
| **S6** Rename the protocol modules | 4.5 | R27-AC4 (performs; S25 verifies) · FI-15 · **HUMAN GATE OQ9** · **M** | `git mv logic/{http,ftp,sftp}.py → logic/{http,ftp,sftp}_client.py` · −`logic/soap.py` (0 bytes; S22 creates `soap_client.py` under its final name — **not** a fourth rename) · ~`logic/__init__.py` + importers · ~`tests/helpers/test_filters_helper.py` (imports only) | **No behaviour change in this commit** — a pure rename, reviewable as one diff · `flake8` reports zero `A005` · `grep -rn "logic/http\.py\|logic/ftp\.py\|logic/sftp\.py\|logic/soap\.py" .` → nothing outside spec/audit · S4's import job still green · **no compatibility alias** (A4: zero consumers) | S5 |

> **S5 — the four `ujson` sites, named.** Three *modules*, **four** *call sites*; `filters_helper.py`
> carries two. Both numbers are used deliberately and are not interchangeable. **A story that migrates
> three sites has not finished.**
> 1. `response_helper.py:14` `ujson.loads` → `orjson.loads` — a test asserts **both** `str` and `bytes` parse.
> 2. `filters_helper.py:45` `ujson.dumps(form_value)` in `FormData.add_field` → `orjson.dumps(v).decode()`
>    — test asserts the field value is `str` and byte-identical to today's.
> 3. `filters_helper.py:78` `data = ujson.dumps(data)` → `.decode()` — test asserts `isinstance(filters['data'], str)`.
> 4. `logic/http.py:30` the `json_serialize` wiring — the default is a **wrapper**
>    (`lambda o: orjson.dumps(o).decode()`), **never bare `orjson.dumps`**. **FI-6:** `ClientSession(
>    json_serialize=)` requires a `str`-returning callable, and this breaks *after* the clean-venv
>    import starts passing, so it looks like the migration succeeded. A test posts a JSON body through
>    the default serializer against the S1 recording handler and asserts the received body.

> **S6 — OQ9 blocks Phase 0, not Phase 6.** FI-15 moved the rename from Step 24 because every test
> path, traceability row and later step in the spec is written in post-rename names. If OQ9 instead
> answers "per-module `# noqa: A005`", **S6 is cancelled, S25 absorbs the suppression + justification,
> and every downstream path in this breakdown reverts** — the breakdown must then be re-issued. Get the
> answer before implementation starts.

### Phase 1 — The contract

| Id | Step | Requirements / findings | File boundary | Definition of done | blockedBy |
|---|---|---|---|---|---|
| **S7** Envelope · timestamps · exceptions · logger · FI-7's HTTP half | 5 | R8 (all but AC2,AC12) · R9 all · R10 (all but AC3) · R6-AC2 · **E1–E11** · C2 H7 H27 H29 M20 MG3 L3 L4 L11 L12 · FI-3, FI-7 · **XL — see §12** | +`utils/{envelope,status_map,redaction}.py` · rewrite `utils/exceptions.py` · ~`helpers/common/date_helper.py`, `utils/constants.py`, `helpers/internal/base.py`, `__init__.py`, `async_gateway.py`, `helpers/internal/request_helper.py`, `logic/http_client.py`, `pyproject.toml` · +`tests/{test_envelope,test_exceptions}.py` | `GatewayResponse` defined **once**; `new_envelope`/`finalise_ok`/`finalise_error` the only constructors; **E1–E11 exist as tests** · `grep -rn "999" async_gateway/` → 0 (E7) · `grep -rn "'tat'"` → 0 · `api_response` absent (test asserts `KeyError`) · `grep -rn "except Exception" async_gateway/` → 0 · **FI-3:** `RetriesExhausted() from ClientConnectorError` yields a non-empty message + `cause` naming the connector; `unwrap_cause` depth-bounded, cyclic chain terminates *(a naive `str(exc)` yields `''`, which reads as success and is worse than 999)* · **FI-7:** delete `request_helper.py:93`'s `kwargs.get('response', {})`; `make_http_request` returns typed `HttpResult`; `grep -rn "kwargs.get('response'"` → 0 · **E11:** a 404 with a JSON error body → `ok False`, `status_code 404`, non-empty `text` **and** populated `json`; `finalise_error` only adds `error` and flips `ok` · UTC ISO-8601 `request_time`; `grep -rn "time.time()"` → 0; clock patched backwards still `latency >= 0` · `grep -rn "Asia/Kolkata\|TIMEZONE\|pytz" async_gateway/` → 0 **and the `pytz` declaration drops in this same commit** (import and declaration must move together or the clean-venv job breaks in one direction) · one redactor serving **both** envelope and logger — header, `?api_key=`, `{'password':…}` and `Set-Cookie` all absent from `repr(result)`; **no `redact=False` exists** · exactly one handler on the `async_gateway` logger and it is a `NullHandler`; `CancelledError` propagates | S6 |
| **S8** Dispatch, validation, the one exception→envelope boundary | 6 | R11-AC1,2,3,4,6,7 · R10-AC3 · H4 H5 H6 L2 · FI-14 · **M** | ~`async_gateway.py`, `logic/__init__.py`, `helpers/internal/base.py` · +`tests/test_entrypoint.py` | protocol normalised **once**, same value for guard and lookup (parametrised: lower/mixed/whitespace × 5 protocols) · `None`/`''`/`123`/unknown → `ConfigurationError` naming supported protocols · `protocol_info` `None` and `{}` work where nothing is required, else `ConfigurationError` naming the key; `base.py:31-37` no longer reads the **raw** `info` after guarding it · **FI-14:** `'HTTPS'`+`http://` → error, schemeless → upgraded, `'HTTP'` accepts either — checked against the URL **actually dispatched**, after pre-processors · registry typed `dict[str, type[BaseRequestClass]]` · **an injected `KeyError`/`TypeError`/`UnboundLocalError` escapes `request()`** rather than becoming an envelope (the property whose absence concealed C6, H6 and most of the audit) | S7 |
| **S9** The contract tests, before the protocol work | 7 | R8-AC2 · R28-AC8 partial · **S** | ~`tests/test_entrypoint.py`, `tests/test_envelope.py` | parametrised no-network assertion `set(result.keys()) == EXPECTED_KEYS` across **5 protocols × {success, failure}** · **expected to FAIL for FTP/SFTP/SOAP on landing — that is the point**; marked `xfail(strict=True)` per protocol so the ratchet stays honest and each protocol story removes exactly its own marker · each test name references the requirement id it proves | S8 |

### Phase 2 — Protocol correctness

| Id | Step | Requirements / findings | File boundary | Definition of done | blockedBy |
|---|---|---|---|---|---|
| **S10** FTP: C6 + H1 + M2 in one commit | 8 | R15-AC1…7,9 *(AC5 README half → S29; AC8 → S19)* · C6 H1 M2 M3 H10-ftp · FI-1 · **M** | ~`logic/ftp_client.py`, `helpers/internal/filters_helper.py` *(`get_ssl_config` only)* · +`tests/logic/{__init__,test_ftp_client}.py`, `tests/fixtures/ftp.py` | **FI-1 is absolute — a story that fixes only the `UnboundLocalError` is rejected.** Fixing C6 makes FTP execute for the first time, putting H1 and M2 on a live socket · `verify_ssl` initialised before the branch; `flake8` reports no `F821` · the SSL block moves **inside** the `try` (a forced `get_ssl_config` failure → populated envelope, `error.code=='TLS'`) · `verify_ssl` defaults **`True`** · **fail closed:** the value passed to `Client.context(ssl=)` is **never `None`** when true, and a non-TLS server fails with `TLS` rather than completing in plaintext · explicit `False` honoured + warning · `stat` no longer unconditional — parametrised `download`/`upload`/`remove` all `ok=True` (a successful delete is a success, not a `999` a retry re-attempts) · connect **and** transfer bounded by `timeout` · `return self.response`; FTP `xfail`s removed | S9 |
| **S11** SFTP transport security, fixture verification-on | 9 | R16-AC1…5 *(AC6 → S29)* · C4 · FI-9 · **S** | ~`logic/sftp_client.py` · +`tests/logic/test_sftp_client.py`, `tests/fixtures/sftp.py` | `grep -n "known_hosts=None" async_gateway/` → 0; default is asyncssh's system resolution achieved by **omitting** the parameter · `known_hosts`/`host_key` pinning; mismatch → `HOST_KEY` (495) · bypass is `insecure_skip_host_key_check=True` **by name**, warns every use; a truthy `verify_ssl=False` does **not** enable it · `client_keys=[]` by default (ambient `~/.ssh/id_*` never offered); supplied list forwarded unchanged · no `~/.ssh/known_hosts` → fails closed naming the option · **FI-9:** the fixture is written with verification **on from the start** — S12's tests are written against it, so it must be right first | S9 |
| **S12** SFTP reports the truth about what it completed | 10 | R17-AC1…6,8,9 *(AC7 → S19)* · H2 M4 M28 L5 L6 H10-sftp · **S** | ~`logic/sftp_client.py`, `tests/logic/test_sftp_client.py` | `remote_files` initialised before the branch; parametrised `get`/`put`/`remove` × {file, directory} → `ok=True` for all six · **destructive success reported as success:** `remove` on a file → `ok=True`, `200`, and **not retried** (asserted by call count) · `self.remote_files` (L6) — dead, mistyped, and the thing that *looks like* the missing initialisation — **deleted in this same commit** so it cannot mislead the next reader · metadata from typed `SFTPAttrs`, with tests for a repr fragment lacking `':'` (today `IndexError`) and a missing `type` key (today `KeyError`) · **M28:** `additional_arguments` copied — a directory `get` then a single-file `remove` **sharing one `protocol_info`** leaves the second without `recurse=True` and the caller's dict unchanged · `connect_timeout`/`login_timeout` from `self.timeout` · `return self.response`; `tat`→`latency`; `mode`/`files`/`file_stats` under `protocol_details`; docstring no longer says "ftp request class" | S11 |
| **S13** HTTP request construction and response handling | 11 | R12 all · R13 all · H14 M5 M6 M7 M8 M9 M10 L8 · **L** | ~`helpers/internal/{filters_helper,__init__,request_helper,response_helper}.py`, `logic/http_client.py` · +`tests/logic/test_http_client.py`, `tests/helpers/test_request_helper.py` · ~`tests/helpers/test_filters_helper.py` | media-type dispatch strips parameters and is case-insensitive; **the same helper serves request and response sides** — the exact-key vs substring inconsistency *is* the evidence of the bug and must not survive · **the spec's filter table exactly**, incl. the **new raw-body branch** for `text/xml`/`application/soap+xml`/`application/xml`; the `'default' → JSON` entry is **replaced** (unknown + `str`/`bytes` → raw, unknown + `dict` → JSON) — routing unknown types to JSON is what sends a SOAP envelope through a JSON encoder · filters take a typed parameter set, **never `kwargs['request_type']`** (today a hard `KeyError` for every SOAP call) · `grep -n "mapping.get(.*)("` shows no un-guarded immediate call · `"get"`/`"GET"`/`"Get"` all attach the payload as **query params** · bool → `"true"`/`"false"` via `isinstance`; `None`/`list`/`datetime`/nested each covered · **no caller dict mutated** (byte-identical after the call) · download keys have documented defaults; omitting both still downloads · dead `if …: pass` removed and the GET+upload case decided explicitly · malformed JSON → `json=None` + `ok=False` + `SERIALIZATION`, legitimate `{}` → `json={}` + `ok=True`, and a literal `null` distinguishable from a parse failure · `UnicodeDecodeError` sets **both** `text` and `error`, no `KeyError` escapes · multipart writes **bytes to a binary file** (`\x89PNG`, not `b'\\x89PNG'`), reads a `chunk_size*3+7` part in full, terminates cleanly on a `None` part, and accumulates into a buffer not `data + str(data)` | S10 |
| **S14** No blocking I/O on an async path + the AST test | 12 | R20 all · C3 · **M** | ~`helpers/internal/request_helper.py`, `utils/http_file_config.py`, `tests/helpers/test_request_helper.py` · +`tests/test_no_blocking_io.py` | all four sites converted (`request_helper.py:69`, `:120,126`, `:185` → a streaming async body which also satisfies R14's retry-safe factory, `http_file_config.py:78` → `aiofiles.os.remove`) · **an AST test over the whole package** asserts no bare `open(` lexically inside any `async def` and **fails when a new one appears**; no sync `os.remove`/`os.path`/`shutil.` inside an `async def` · **loop fairness as a counting assertion:** 256 mocked chunks vs an `asyncio.sleep(0)` observer under `gather`, asserting **≥ 256** round-trips — deterministic, no clock read, compatible with R28's no-sleep rule · no new dependency (`aiofiles` is already used correctly ~30 lines away) | S13 |

> **⚠ S10 owns the A6 monitoring obligation — an explicit checkpoint, not a footnote.**
> The premortem of record names A6 as the likeliest remaining failure: `asyncssh`/`aioftp` driven to
> 100% **branch** coverage through mocks alone, where the `aiohttp.web` answer gives nothing.
> **At Step 8, measure branch coverage of `logic/ftp_client.py` in isolation**
> (`pytest tests/logic/test_ftp_client.py --cov=async_gateway.logic.ftp_client --cov-branch`) and
> **record the number in the ticket work log**. **Threshold ≈ 90%.** A plateau below ~90% without a
> live server means **A6 is failing** and the containerised-fixture cost arrives at **S27 (Step 26)**
> instead of here. **On a sub-90% plateau: stop and escalate to the orchestrator before S11 starts.**
> Do not absorb it with `# pragma: no cover` — the ceiling of 10 is the only thing between that and a
> hollow 100, and spending it here is exactly how the hollow 100 happens.

### Phase 3 — Resilience, security and utilities

| Id | Step | Requirements / findings | File boundary | Definition of done | blockedBy |
|---|---|---|---|---|---|
| **S15** HTTP transport resilience + the library-owned redirect loop | 13 | R14 all · R21-AC6 · H9 H10-http H11 H12 MG4 M21-in-scope · FI-16 · **L** | ~`logic/http_client.py`, `helpers/internal/request_helper.py`, `utils/http_file_config.py`, `utils/constants.py`, `tests/logic/test_http_client.py`, `tests/helpers/test_request_helper.py`, `tests/fixtures/http_server.py` | caller-supplied `session` used and **not closed**; created+closed when absent; two requests on one session reuse a connection (via `on_connection_reuseconn`) and it stays open; a closed session → `ConfigurationError` · `grep -n "ClientSession("` shows **no** construction without `timeout=` · per-protocol timeout → `TIMEOUT`/`504` within the deadline, driven by a handler awaiting an `asyncio.Event` released in teardown with `timeout` in tens of ms — *the test waits only on the deadline it asserts, which is the behaviour under test, not the sleep-to-settle pattern R28 forbids* · `max_response_bytes` **64 MiB** default, per-call override, no disable sentinel; over-cap → `RESPONSE_TOO_LARGE` without allocating the body; enforced on **every** read path (in-memory, streamed download, and SOAP from S22) — `Content-Length` over cap rejected **before** the read, chunked rejected mid-stream *(`request_helper.py:34` excluded: it is inside `fetch_file`, which S20 deletes)* · **H9:** body from a **factory invoked per attempt**; one failure then one success and attempt 2's body length equals the file's real length (a zero-byte upload is legitimate, so the assertion is on the real length, not "non-zero") · chunk default 64 KiB · **FI-16 — the redirect loop and the per-hop check are one fix:** `allow_redirects=False` on the transport, the library follows, bounded by `max_redirects` (10); **every** `Location` resolved and re-checked against `allowed_schemes` **before** the next request, a rejection raising `CONFIG` and naming scheme + hop. Tests: 302→`ftp://` refused with the fixture's **recorded request count staying at 1**, 302→`file://` likewise, a relative `Location` resolved and followed, `max_redirects+1` fails. *A story that surfaces `max_redirects` without the per-hop check, or asserts the check without owning the loop, is rejected.* | S14 |
| **S16** Breaker facade + per-destination registry + clock/sleep seam | 14 | R24 all · R7-AC1,AC7 · H8 M12 M13 M14 M15 M16 L9 L10 · FI-4 · **L** | +`helpers/internal/breaker_registry.py`, `tests/helpers/test_circuit_breaker.py`, `tests/fixtures/clock.py` · rewrite `helpers/internal/circuit_breaker_helper.py` · ~`helpers/internal/base.py`, `utils/constants.py` | **FI-4 — H8 and M16 land together;** hoisting to a module global without a per-destination registry trades a dead breaker for one flaky host opening the circuit for every destination, a worse outage than today's · state survives across `request()` calls: `maximum_failures+1` failures → `CIRCUIT_OPEN`/`503` **without reaching the transport** · keyed `(family, host, port)`; opening A leaves B reaching the transport; bounded LRU (default 256) by least-recent **use** · **the seam lives in this library's facade (Ruling D):** `pyfailsafe` provably has none (`circuit_breaker.py:139,142`, `failsafe.py:103`), so the facade **owns the whole breaker state machine and the whole retry loop** — counting, transitions, the three callback invocations, the backoff wait — delegating **only** exception classification and backoff computation. `clock`/`sleep` are keyword-only with real defaults and are **not** part of the LRU key; `reset()` clears between tests. A full open→half-open→close cycle runs on a `FakeClock` + recording `sleep` with `time.monotonic` and `asyncio.sleep` **untouched** · **this story does not wait on S23** — the facade's interface is identical under keep/replace/vendor, which is what takes S27 off an open dependency decision · half-open tested by **advancing the injected clock**, never by sleeping · non-zero `delay`/`max_delay`, `jitter=True`, exponential default; recorded durations show three retries spaced and two sequences **not synchronised** · `backoff: Literal['constant','exponential']` replaces the magic `name=='backoff'` · three parametrised config typos each raise `ConfigurationError` **naming the key** · `maximum_failures=0` reachable; numerics accept `int`/`float`, **reject `bool`** (`0`, `True`, `1.5`, `"5"`) · **M12:** two requests sharing one config leave the caller's dict unchanged · **R7's seven fitness behaviours exist as tests here**, behaviour 7 asserted **against the facade** (a test against the dependency would be a test of a known-false proposition) | S15 |
| **S17** A client TLS context that is actually a client TLS context | 15 | R23 all · H28 M1 · FI-2 · **S** | ~`helpers/internal/filters_helper.py` *(`get_ssl_config`/mTLS context only)*, `tests/helpers/test_filters_helper.py` · +`tests/fixtures/tls.py` | `Purpose.SERVER_AUTH` then `load_cert_chain`; `grep -n "Purpose.CLIENT_AUTH"` → 0 · the implementation **asserts** `check_hostname is True` and `verify_mode == CERT_REQUIRED` before returning, so a refactor cannot silently weaken it; a test asserts both · returned under `ssl=`, not the deprecated `ssl_context=`; a test asserts the keyword actually passed to the connector · `{'ssl': verify_ssl or True}` — always `True`, so the flag is inoperative — replaced by an explicit decision (`False` disables + warns, `True` enables, absent → `True`). *This turns an accident into a decision; the obvious cleanup refactor would have turned it into a live MITM switch (M1).* · **FI-2 — the mTLS test is not optional:** this fix makes client certificates connect **for the first time ever**, so a throwaway cert pair + a **loopback TLS server requiring client auth** must assert a real handshake through `get_ssl_config`, and a wrong/expired cert must fail with `TLS` · a missing/unreadable/mismatched cert file → `ConfigurationError` **naming the file**; a passphrase-protected key → `ConfigurationError` naming the limitation · written **once** against aiohttp 3.14.x — no migration, no re-run gate | S15 |
| **S18** Path containment and safe local file handling | 16 | R22 all · M17 M18 M19 · **M** | +`utils/paths.py`, `tests/utils/{__init__,test_paths}.py` · ~`utils/http_file_config.py`, `helpers/internal/request_helper.py`, `logic/ftp_client.py`, `logic/sftp_client.py` | one `resolve_within(base, candidate)` canonicalises and asserts containment **before any write**; every write path routes through it · parametrised traversal table (`../evil`, `../../etc/passwd`, `/absolute/path`, `a/../../b`, a null byte, an outward symlink) each → `PATH` and **writes nothing**, with the target directory asserted empty afterwards *(on a recursive download the **remote server** supplies the names — the Zip-Slip-shaped primitive, made likelier by C4)* · `safe_write` uses `O_NOFOLLOW` + restrictive mode; a pre-created symlink is **refused, not followed**; default **refuse to overwrite** (`O_EXCL`) with opt-in `overwrite=True` · where `O_NOFOLLOW` is unavailable, degrade to an explicit pre-write `lstat` check **with the same coverage**, documented · `safe_unlink` idempotent (called twice on an absent file, no exception) · a `try/finally` removes partial files — a forced mid-stream failure leaves none | S15, S10, S12 |
| **S19** Verb allowlists and the URL-trust contract | 17 | R21-AC1,2,5 · **R15-AC8** · **R17-AC7** · M25 H13-ptr M21-partial · **L** *(serialization point — 5 `getattr` sites in 5 files)* | ~`helpers/internal/base.py`, `logic/{http,ftp,sftp}_client.py`, `helpers/internal/request_helper.py`, `utils/http_file_config.py`, `tests/logic/test_ftp_client.py`, `tests/logic/test_sftp_client.py`, `tests/helpers/test_request_helper.py` | each protocol declares an explicit allowlist; `grep -n "getattr("` shows **every remaining call preceded by an allowlist check in the same function** · unknown verb → `ConfigurationError` naming the verb and listing allowed values — **fail closed**; parametrised over **all five sites** (`sftp_client.py:43`, `ftp_client.py:51`, `request_helper.py:31`, `:100`, `http_file_config.py:57`) · `request_type='close'` — today `TypeError` swallowed as `999` — is now `ConfigurationError` · a verb that exists but is not a coroutine function is covered; verb case/whitespace normalisation matches S8's · `allowed_schemes` defaults `{'http','https'}` and rejects the rest; `ftp://`/`file://`/`javascript:` passed to `'HTTP'` tested *(per-hop enforcement already landed in S15 — FI-16)* · **closes R15-AC8 and R17-AC7**, whose text forward-references "the R21 allowlist" from Steps 8 and 10 — see §11-D3 | S16, S18 |
| **S20** File-transfer utilities that work and exist once | 18 | R25-AC1,2,3,4,5,7(export half) · H15 H16 MG5 L13 · **M** | −`helpers/common/file_helper.py` · ~`helpers/internal/request_helper.py` *(delete `fetch_file`)*, `utils/http_file_config.py`, `utils/constants.py` · +`tests/utils/test_http_file_config.py` | the duplicate module and the unreferenced `fetch_file` are **deleted**; a test asserts the module no longer imports *(the duplicate is H15's second copy with a **different parameter order**, so importing the wrong one and calling positionally writes the bucket name to a local path)* · one `download_file_from_s3` on `aioboto3.Session().client(...)` with the correct **`Filename=`**; a mocked test asserts the exact `download_file(Bucket=, Key=, Filename=)` signature · parameters **keyword-only** so a positional mistake is a `TypeError` at the call site · URL download raises on **any** non-success status — parametrised 200/301-followed/400/403/404/500/502, only success writes, every failure gives `ok=False` + the status and **no file on disk** *(today only 403 is checked, so a 404 body or HTML login redirect is saved as the requested file and any later upload ships the error page)* · `STATUS_CODE_403` removed · absent S3 credentials wrapped in `ConfigurationError` · a zero-length success body is written and documented as legitimate · FI-11: written **once** against aioboto3 15.x; no test exercises the S3 path earlier | S19 |
| **S21** A request tracer whose results belong to one request | 19 | R26 all · R30-AC6 · H17 M11 M20-part · **M** | ~`utils/request_tracer.py`, `logic/http_client.py`, `utils/envelope.py` · +`tests/utils/test_request_tracer.py` | results live on the per-request trace **`context`**, collected into the envelope at the end — never on the shared `TraceConfig`; **two concurrent requests through one caller-supplied `trace_config` under `gather`** each carry only their own events (no interleaving, no stale `on_request_exception`) · `on_connection_reuseconn` records a **relative delta** like its 13 siblings and does **not** overwrite the baseline; a reused connection still measures from the original start *(today it under-reports latency on every keep-alive connection and stores an absolute timestamp among 13 relative deltas)* · `on_request_exception` stores a **string** via S7's `unwrap_cause`, never a live exception; a test asserts `isinstance(..., str)` and no `Authorization` value *(a `ClientResponseError` carries `.request_info.headers`)* · **all 15** callbacks annotated and each exercised at least once (`request_tracer.py:111-125` — **15**, not the 14 the audit's prose said) · a foreign `trace_config` must not be assumed to have `results_collector`; `trace_config=[]` → `[]`, not `KeyError`; a failure before `on_request_start` covered | S15 |

### Phase 4 — SOAP

| Id | Step | Requirements / findings | File boundary | Definition of done | blockedBy |
|---|---|---|---|---|---|
| **S22** `logic/soap_client.py`, from scratch | 20 | R18-AC1…8 · R19 all · R11-AC5 · R27-AC6 · H3 · **L — see §12** | +`logic/soap_client.py`, `tests/logic/test_soap_client.py` · ~`logic/__init__.py`, `helpers/internal/{__init__,filters_helper,request_helper}.py`, `utils/{exceptions,status_map}.py`, `tests/test_entrypoint.py` *(remove SOAP `xfail`s)* | `'SOAP'` dispatches to a real `SoapRequest`; `grep -rn "'SOAP': None"` → 0; mypy no longer reports `"None" not callable` · `soap_version` ∈ {1.1 default, 1.2}, else `ConfigurationError` · version-correct namespaces (`schemas.xmlsoap.org/soap/envelope/` · `www.w3.org/2003/05/soap-envelope`) + optional `soap_headers` inside `<Header>`; an already-complete `<Envelope>` is **not double-wrapped** · body accepted as an XML **string** or an `Element`; **no dict-to-XML mapping** · headers asserted off the recording handler: **1.1** `text/xml; charset=utf-8` **and** `SOAPAction` emitted **always** (`""` when absent, quoted when supplied); **1.2** `application/soap+xml; charset=utf-8` with `;action="…"` and **no** `SOAPAction` (OQ5) · **byte-identity:** the body bytes equal `build_envelope(...).encode('utf-8')` exactly — dispatched through S13's **raw-body filter**, never the JSON filter — asserted via `await request.read()` on the real loopback handler for both versions · `soap_body` = **the first element child of `<Body>`**, `None` when absent/empty/empty-response, first-with-a-`warning` when several (1.1 permits several; raising would reject legitimate traffic) · reuse not reimplementation: same session/timeout/**capped reader** (S15), breaker (S16), tracer (S21), request path (S13), envelope (S7) — a test asserts `request_tracer` populated, `timeout` honoured, over-cap → `RESPONSE_TOO_LARGE` · Faults for both versions (incl. the 1.2 `Code/Subcode` chain) → `ok=False`, `SOAP_FAULT`, structured `SoapFault`; **a Fault at HTTP 200 still yields `ok=False`**; `status_code` is the real status, nothing synthesised · **DOCTYPE rejection is prolog-scoped** — before the root element's start tag, by prolog scan or an `expat` `StartDoctypeDeclHandler`, **before** `fromstring`; **a substring search over the body is explicitly forbidden**. Two tests, both required: billion-laughs → `XML_UNSAFE`, memory flat, `fromstring` never called; **and** a well-formed response whose `<detail>` contains the literal text `DOCTYPE` is **accepted and parsed normally** · malformed XML and valid-but-not-SOAP (an HTML proxy page) → `SERIALIZATION` with position/first bytes, never `{}` · `multipart/related` (MTOM) → `ConfigurationError`, never mis-parsed · `grep -rn "lxml" async_gateway/` → 0 · SOAP rows of S9 go green; all five protocols now satisfy E1 and E11 | S17, S20, S21 |

### Phase 5 — The resilience-library decision

| Id | Step | Requirements / findings | File boundary | Definition of done | blockedBy |
|---|---|---|---|---|---|
| — | **21** | ***vacated*** — the dependency upgrade moved to Step 1.5 (S2). Retained as an empty slot, never reused or renumbered, so every citation of "Step 21" still points somewhere truthful. **No story.** | — | — | — |
| **S23** The resilience-library decision + ADR | 22 | R7-AC2,3,4,5,6 (AC1 run-in-CI half) · MG1 · **HUMAN GATE OQ12** · **S under KEEP** | **Under KEEP:** +`docs/decisions/0001-resilience-library.md` · ~`.github/workflows/ci.yml`, `tests/helpers/test_circuit_breaker.py`. **Under REPLACE/VENDOR:** additionally `helpers/internal/circuit_breaker_helper.py`, `pyproject.toml` — **a different story; see the box below** | behaviours **1–6** run against `pyfailsafe` on the S2 set **across every interpreter in the R1 matrix** (`3.10–3.14`) — dormancy bites at the ceiling, not the floor, and a run on the floor alone does not discharge the criterion · **behaviour 7 is NOT re-evaluated** — it is known-failed by execution, and a Step-22 "evaluation" re-opening a question already closed does not discharge the criterion; the ADR carries the three grepped lines (`circuit_breaker.py:139,142`, `failsafe.py:103`) **verbatim** · the ADR records: option chosen, the matrix output pasted in, options rejected and why, **the named cost verbatim** (the facade owns the whole breaker state machine and the whole retry loop; the dependency supplies exception classification and backoff computation only — `retry_policy.py`, **136 of 539 lines, ~40 non-trivial**), owner = the maintainer, revisit trigger = the first reach into `failsafe` internals or the first behaviour to regress on a new interpreter. **An ADR recording a *larger* residual has failed this criterion, not rounded it.** · the human's answer to OQ12 is recorded in the same ADR · if the costing shows the residual smaller still, the ADR **says so and recommends vendoring** rather than shipping a facade that has quietly become a re-implementation · if a breaker is vendored: **≤ 250 lines**, no new dependency, 100% line+branch · if a dependency is adopted: `library-review` completed in the ADR + recorded user approval | S16, S22 |

> **S23 — OQ12 is a human decision on the critical path, and it changes this story's shape.**
> Ruling D pre-decides **keep-with-named-cost** and S16's facade lands at Step 14 regardless, so the
> *decision* blocks nothing in Phases 0–4. But the *answer* determines S23's file boundary and
> therefore whether it can run beside S24:
> - **KEEP** (the spec's ruling) → S23 is a docs + CI story, **S** sized, fully disjoint from S24.
> - **REPLACE or VENDOR** → S23 rewrites `circuit_breaker_helper.py` four steps upstream of S27's
>   terminal 100% gate, and **the spec carries no scoped steps for either branch** (OQ12: *"each of
>   which needs its own scoped steps, which this plan does not currently carry"*). That is a re-plan,
>   not a bigger story. **Escalation E-2 (§11).**
>
> **Ask OQ12 at ticket creation (Stage 1g), not at Wave 17.** No recommendation is offered here — the
> spec deliberately withholds one, and locked decision 5 ("no silent keep, no silent swap") makes this
> the human's call rather than the plan's.

### Phase 6 — Gates on, one per commit

| Id | Step | Requirements / findings | File boundary | Definition of done | blockedBy |
|---|---|---|---|---|---|
| **S24** Docstrings and full type annotations | 23 | R30 all · L1 L2 L4 L5 L7 L14 + annotation coverage · FI-8 · **XL — optional split, §12** | ~every `.py` under `async_gateway/` · ~`pyproject.toml` *(strictness flags)* · +`tests/test_docs.py` | every module docstring is **≥ 40 characters and ≥ 6 whitespace-separated words** and is not merely the filename, case- and punctuation-insensitive — a one-line `"""Internal."""` **must fail**, and `"""Ftp."""` (4 chars, 1 word), `"""Constants."""` and the two 0-byte `__init__.py` files fail decisively · every public function/method/class documents args, returns and raises · `disallow_untyped_defs` + `disallow_incomplete_defs` enabled for `async_gateway` and passing · no bare `dict`/`list`/`Dict`/`List` as a public return type · `handle_request()` on the base and **all four** overrides → `-> GatewayResponse` · the six named defects fixed (`sftp_client.py:13`, `request_helper.py:88`'s Sqlalchemy param, `base.py:32`, `http_client.py:26`'s `List[TraceConfig()]`, `async_gateway.py:97,105,108`'s annotated subscript targets — deleted) · `typing.Text` nowhere · **FI-8: this lands BEFORE S25 un-suppresses the docstring rules**, or a large baseline is created for a class of finding about to be fixed wholesale — and baselines that large are the ones that become permanent | S22, S21 |
| **S25** flake8 to zero | 24 | R27-AC2,3,7,8 · R27-AC4 *(verifies S6)* · H19 L15 · **M** | ~`pyproject.toml`/`.flake8`, `.github/workflows/ci.yml` · ~the files flake8 reports (L15's five: `base.py`, `filters_helper.py`, `circuit_breaker_helper.py`, `ftp_client.py`, `sftp_client.py`) — **lint conformance only, no behaviour change** | fresh venv `pip install -e '.[dev]' && flake8 .` exits 0 with no output · `application_import_names = async_gateway` (underscore) — today an illegal identifier, so import-order checking has been checking a fiction · one quote style chosen and formatter-enforced so the 938 quote findings become a formatter concern, not a lint backlog · the baseline suppression file shrinks to zero and is **deleted** · **the `A005` rename is NOT here — S6 performed it; this step verifies:** `flake8` reports zero `A005` with **no `# noqa` anywhere** · a CI step greps for bare `# noqa`/`# type: ignore` without a rule code or a `--` justification and **fails** · a formatter runs in CI in check mode | S24 |
| **S26** Remove `mypy ignore_errors`; drive to zero; then `py.typed` | 25 | R27-AC5,6 · R4-AC5 · H18 · FI-13 · **M** | ~`pyproject.toml`, `.github/workflows/ci.yml` · +`async_gateway/py.typed` · ~`tests/test_packaging.py` · ~the files mypy reports — **type conformance only** | `grep -rn "ignore_errors" .` returns nothing outside the spec and the audit report; `mypy async_gateway` exits 0 · `ignore_missing_imports` only per-module, each with a comment naming the package — **never globally**, or the gate is back where it started · **`py.typed` is added only now, last** (FI-13): publishing it before mypy is clean exports this library's type errors to every downstream consumer's build · a packaging test asserts `py.typed` is in the wheel · the count discrepancy (40 vs 44, OQ2) **is not allowed to matter** — the criterion is count-independent | S25 |
| **S27** Coverage ratchet to its terminal value | 26 | R28-AC4,5,6,7,8 · H20 · **M** | ~`pyproject.toml`, `.github/workflows/ci.yml`, `tests/**` — **no source file may be edited.** If source must change to be testable, that is a defect loop back to the owning story, not a widening of this boundary | `pytest` exits 0 at **exactly 100/100 line+branch** on a clean checkout and non-zero at 99.9% · pragma count **≤ 10** and every pragma carries a `--` justification; two CI checks enforce both · the **outbound**-network-disabled run passes with `127.0.0.1` allowlisted (two fixtures bind loopback: the `aiohttp.web` server and S17's TLS server) · `pytest -p randomly` green on all five committed seeds · every criterion in the spec that names a test has that test, named for its requirement id · **this step is a finish, not a flip** — if `fail_under` is still far below 100 when it begins, the ratchet was not maintained and that is a **process failure to escalate, not a number to negotiate down** | S26 |

### Phase 7 — Release

| Id | Step | Requirements / findings | File boundary | Definition of done | blockedBy |
|---|---|---|---|---|---|
| **S28** Version, provenance, Sphinx retirement, LICENSE | 27 | R5-AC1,2,3,5,6 · R31-AC1/AC2,AC3 · R34-AC1,2,3 · H22 H25 M24 · **HUMAN GATES OQ4, OQ7** · **M** | ~`pyproject.toml`, `async_gateway/__init__.py`, `LICENSE`, `tests/test_packaging.py` · −`docs/source/conf.py`, `docs/make.bat`, `docs/source/Makefile` · +`tests/data/mit-reference.txt` | version `1.0.0` declared in **exactly one place**; a test asserts `importlib.metadata.version("async-gateway") == async_gateway.__version__` · `grep -rn "2\.7\.3" .` → nothing outside the audit report, the spec and the CHANGELOG history · `download_url` gone from all metadata *(it points at **a different project's** release tarball)*; `project.urls` resolves only to this repository · the three Sphinx files deleted and `sphinx`+`sphinx-rtd-theme` dropped from the dev set (**or** the documented alternative: an `index.rst` root, the Makefile at `docs/`, `make -C docs html` green in CI — exactly one branch is discharged and the other recorded not-applicable); **`docs/specs/` and `docs/decisions/` survive — only the three Sphinx files go** · MIT text byte-identical to the committed reference apart from the copyright line; `license` field, classifier and file agree · **OQ4:** re-verify the PyPI 404 immediately before this story and record it in the work log — the reset is a one-way door and is safe only while it holds · **OQ7:** the copyright line is a **human decision**, recorded; an agent cannot make it · the git tag is **S32's**, not this story's | S27 |
| **S29** README rewrite, with its five mechanical tests | 28 | R29 all · R21-AC3,AC4 · R8-AC12 · R6-AC7 · R5-AC7 · R15-AC5(doc half) · R16-AC6 · R18-AC9 · R25-AC6,AC7(doc half) · R34-AC4 · H24 MG6 MG8 · **S** | ~`README.md`, `tests/test_docs.py` | named sections: Install · Quickstart per protocol · Public API reference · The response envelope (R8's full key set, types, per-protocol `status_code`) · Error handling (R10's hierarchy + code table) · Retry/timeout/circuit-breaker · Transport security · **You own URL validation** · Supported Python versions · Versioning policy · Contributing · Changelog link. *(A deliberate, stated divergence from `documentation.md` §1's service-shaped template: this is a library with no endpoints, env vars, services or health check.)* · **every ```python block `compile()`s**; **every documented `from async_gateway… import` resolves**; **every documented call matches `inspect.signature`** · the payload-echo disclosure is present and tested (masked by key name to depth **4**; below that and for non-mapping payloads the caller's data is echoed **verbatim**) — this is the mechanical home for R8/E9's "and the README says so plainly" · the SOAP section carries all **three** consumer facts, each asserted: hand-built XML string or `Element` with no dict-to-XML; `soap_body` is a raw `Element`, not a mapping, shown with one line of ElementTree; MTOM unsupported and raising — the mechanical home for R18's "documented as out of scope" · `grep -c "api.fyndx1.de" README.md` → **0** · `grep -ci "sftp"` > 0 with host-key verification, pinning, the named bypass and key auth documented · `999` appears nowhere as a status · a test asserts every `protocol_info` key the code reads appears in the README **and vice versa** (the anti-drift criterion — not the length) · every allowlisted verb appears in the README and vice versa · the release section names **one** file to bump · the licence and attribution are stated | S27 · *(reads OQ7's answer; not blocked on S28's code)* |
| **S30** Correct the repository's own agent-facing instructions | 29 | R32 all · MG7 · **HUMAN GATE OQ8** · **S** | ~`CLAUDE.md`, `.claude/rules/fastapi-patterns.md` | the Commands block names the real commands: `pip install -e '.[dev]'` · `pytest` · `flake8 .` · `mypy async_gateway` · the chosen formatter · `python -m build`. **There is no "Run" command and the block says so explicitly** — this is a library, not a service · the FastAPI resource recipe is replaced by the adding-a-protocol recipe (registry entry → protocol class → envelope mapping → error mapping → tests → README section) · `grep -n "uvicorn\|app.main\|ruff\|app/" CLAUDE.md` → no stale references · **explicit user approval is recorded in the work log before the edit** — both files are on the project-wide list. **If the human declines, MG7 is recorded "rejected by human" in the traceability table, not silently dropped**, and this story closes as rejected rather than skipped | S27 · *fully file-disjoint from all 31 others (§7)* |
| **S31** CHANGELOG and examples | 30 | R33 all · R35 all · R31-AC4 · **M** | +`CHANGELOG.md`, `examples/{http,ftp,sftp,soap,error_handling}_example.py` · ~`.github/workflows/ci.yml`, `tests/test_packaging.py` | Keep-a-Changelog with a `1.0.0` entry stating plainly that the package was **never published**, that the version moved **down** from the fork-inherited 2.7.3, and why that is safe · breaking changes listed **as breaking**: the single envelope, removal of `api_response`, `tat`→`latency`, the FTP `verify_ssl` default flip, SFTP host-key verification on by default, the `logic/*` renames · records the **R7 decision** (S23's ADR) and the **R31 docs decision** (S28) · the `Development Status` classifier choice justified here (R4-AC6's discharge point) · versioning policy stated once and cross-linked with the README · a CI step fails a PR touching `async_gateway/` without touching `CHANGELOG.md`, with a documented `skip-changelog` escape · five examples, each **≤ ~40 lines**, one thing each, syntax-checked in CI, **no third-party or corporate endpoint** · wheel/sdist inclusion decided explicitly and asserted by a packaging test | S28, S23 |
| **S32** Final gate run and the tag | 31 | R1-AC10 · R5-AC4 · R4/R27's five-item PR gate · **S** | no file edits; verification + one annotated tag | the **five-item PR gate** passes on a clean checkout: fresh-venv install of the built **wheel** imports a submodule · linter zero violations · mypy zero errors with **no `ignore_errors`** · full suite green at **100% line + branch** · `sdist` + `wheel` build and pass `twine check` · every CI step is a **required check** and none is `continue-on-error` · an annotated **`v1.0.0`** tag on the release commit, and it is the repository's **first** tag · **nothing is uploaded** — the PyPI upload is a manual human action, out of scope (locked decision 4) | S29, S30, S31 |

---

## 5. Fix-interaction constraints → owning story

Every FI is discharged inside one story, or by an ordering edge — never left to anyone's memory.

| FI | Constraint | Discharged by |
|---|---|---|
| FI-1 | C6 + H1 + M2 in one commit | **S10** (a story fixing only the `UnboundLocalError` is rejected) |
| FI-2 | R23's mTLS live-handshake test is not optional | **S17** |
| FI-3 | `unwrap_cause`, not `str(exc)` | **S7** |
| FI-4 | H8 + M16 together | **S16** |
| FI-5 | delete `coverage_output.py` before `--cov-branch` | **S1** (same commit) |
| FI-6 | wrap the default `json_serialize` to return `str` | **S5** (site 4) |
| FI-7 | the three envelopes are one seam defect | **S7** (HTTP half explicitly, `request_helper.py:93`) |
| FI-8 | R30 before R27's docstring rules | edge **S24 → S25** |
| FI-9 | the SFTP fixture is written verification-on | **S11** |
| FI-10 | R6 lands before R14/R23/R25/R26 | edge **S2 → S15, S17, S20, S21** (Phase 0) |
| FI-11 | R6 and R25 must not disagree about the S3 API | **S2**'s import gate + contingency; **S20** written once |
| FI-12 | the security baseline is absolute, not a delta | **S2** |
| FI-13 | `py.typed` only after mypy is clean | edge **S26** (S3 explicitly excludes it) |
| FI-14 | the scheme check runs after pre-processors | **S8** |
| FI-15 | the rename cannot land late | **S6** (Step 4.5), verified at **S25** |
| FI-16 | `allowed_schemes` + the redirect loop are one fix | **S15** |

---

## 6. Dependency graph and acyclicity

```
S1 → S2 → S3 → S4 → S5 → S6 → S7 → S8 → S9 ─┬─→ S10 → S13 → S14 → S15 ─┬─→ S16 ─┐
                                            └─→ S11 → S12 ──┐          ├─→ S17 ─┤
                                                            ├──────────┴─→ S18 ─┤
                                                            │                   ├─→ S19 → S20 ─┐
                                                            │                   │              │
                                                            │      S15 ─────────┴─→ S21 ───────┤
                                                            │                                  │
                                                            └──────────────────────────────────┴─→ S22
S22 ─┬─→ S23 ──────────────────────────────────────────────────────────────────────────┐
     └─→ S24 → S25 → S26 → S27 ─┬─→ S28 ─┬─→ S31 ←──────────────────────────────────────┘
        (S21 → S24 also)        ├─→ S29 ─┤
                                └─→ S30 ─┴─→ S32
```

**Edge list (blockedBy).** S1:— · S2:S1 · S3:S2 · S4:S3 · S5:S4 · S6:S5 · S7:S6 · S8:S7 · S9:S8 ·
S10:S9 · S11:S9 · S12:S11 · S13:S10 · S14:S13 · S15:S14 · S16:S15 · S17:S15 · S18:S15,S10,S12 ·
S19:S16,S18 · S20:S19 · S21:S15 · S22:S17,S20,S21 · S23:S16,S22 · S24:S22,S21 · S25:S24 · S26:S25 ·
S27:S26 · S28:S27 · S29:S27 · S30:S27 · S31:S28,S23 · S32:S29,S30,S31.

**Acyclicity — proof, not assertion.** Assign each story its spec **Step** number
(1, 1.5, 2, 3, 4, 4.5, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 22, 23, 24, 25, 26,
27, 28, 29, 30, 31 — a strict total order; 21 is vacated and unassigned). Inspecting all 44 edges
above, **every edge runs from a strictly lower step to a strictly higher step**; there is no edge with
equal or descending step numbers. A directed graph that embeds in a strict total order cannot contain
a cycle, since a cycle would require some edge to descend. **The graph is acyclic.** The same property
is what makes C-3 (merge order = step order) a valid topological order.

Independently checked: no story appears in its own transitive `blockedBy` closure, and every story is
reachable from S1 (no orphans) and reaches S32 (no dead ends).

---

## 7. Parallel-lane plan — what the orchestrator executes

25 waves. Merge order inside a wave is always ascending step number (C-3). Lanes are git worktrees per
`.claude/rules/mandatory-workflow.md` §2a, removed after the gates pass.

| Wave | Lanes | Stories (step) | Per-lane file boundary — **mutually disjoint** |
|---|---|---|---|
| W0–W5 | **1** | S1(1) → S2(1.5) → S3(2) → S4(3) → S5(4) → S6(4.5) | Serial by construction: S1 writes the config S2 verifies against, S2's set feeds S3's `pyproject.toml`, S3 deletes the `setup.cfg` S1 wrote, S4 stands on S3's artifacts, S5 edits `logic/http.py` which S6 renames. **No parallelism exists in Phase 0.** |
| W6–W8 | **1** | S7(5) → S8(6) → S9(7) | S7 and S8 share `async_gateway.py` + `base.py`; S9 extends S8's test module. Serial. |
| **W9** | **2** | **α** S10(8) ‖ **β** S11(9) | α `logic/ftp_client.py`, `helpers/internal/filters_helper.py`, `tests/logic/test_ftp_client.py`, `tests/fixtures/ftp.py` — β `logic/sftp_client.py`, `tests/logic/test_sftp_client.py`, `tests/fixtures/sftp.py` |
| **W10** | **2** | **α** S13(11) ‖ **β** S12(10) | α `helpers/internal/{filters_helper,__init__,request_helper,response_helper}.py`, `logic/http_client.py`, `tests/logic/test_http_client.py`, `tests/helpers/{test_filters_helper,test_request_helper}.py` — β `logic/sftp_client.py`, `tests/logic/test_sftp_client.py`. *(Merge β at step 10 before α at step 11.)* |
| W11 | 1 | S14(12) | `helpers/internal/request_helper.py`, `utils/http_file_config.py`, `tests/test_no_blocking_io.py`, `tests/helpers/test_request_helper.py` |
| W12 | 1 | S15(13) | the transport hub — `logic/http_client.py`, `helpers/internal/request_helper.py`, `utils/{http_file_config,constants}.py` + its tests and `tests/fixtures/http_server.py` |
| **W13** | **3** | **α** S16(14) ‖ **β** S17(15) ‖ **γ** S18(16) | α `helpers/internal/{breaker_registry,circuit_breaker_helper,base}.py`, `utils/constants.py`, `tests/helpers/test_circuit_breaker.py`, `tests/fixtures/clock.py` — β `helpers/internal/filters_helper.py`, `tests/helpers/test_filters_helper.py`, `tests/fixtures/tls.py` — γ `utils/paths.py`, `utils/http_file_config.py`, `helpers/internal/request_helper.py`, `logic/{ftp,sftp}_client.py`, `tests/utils/test_paths.py`. **Pairwise disjoint — verified α∩β, α∩γ, β∩γ all ∅.** |
| W14 | 1 | S19(17) | **serialization point** — five `getattr` sites across `base.py`, `logic/{http,ftp,sftp}_client.py`, `request_helper.py`, `http_file_config.py` |
| **W15** | **2** | **α** S20(18) ‖ **β** S21(19) | α `helpers/common/file_helper.py`(DEL), `helpers/internal/request_helper.py`, `utils/{http_file_config,constants}.py`, `tests/utils/test_http_file_config.py` — β `utils/{request_tracer,envelope}.py`, `logic/http_client.py`, `tests/utils/test_request_tracer.py`. Disjoint ✅ |
| W16 | 1 | S22(20) | the SOAP hub — `logic/{soap_client,__init__}.py`, `helpers/internal/{__init__,filters_helper,request_helper}.py`, `utils/{exceptions,status_map}.py`, its tests |
| W17 | 1 | S23(22) | `docs/decisions/`, `ci.yml`, `tests/helpers/test_circuit_breaker.py` — **under KEEP only**; see §11-E2 |
| W18 | 1 *(or 4 — §12)* | S24(23) | every `.py` under `async_gateway/`, `pyproject.toml`, `tests/test_docs.py` |
| W19–W21 | 1 | S25(24) → S26(25) → S27(26) | Each is a whole-tree gate over the output of the last. Inherently serial. |
| **W22** | **3** | **α** S28(27) ‖ **β** S29(28) ‖ **γ** S30(29) | α `pyproject.toml`, `async_gateway/__init__.py`, `LICENSE`, `docs/{make.bat,source/*}`, `tests/{test_packaging.py,data/mit-reference.txt}` — β `README.md`, `tests/test_docs.py` — γ `CLAUDE.md`, `.claude/rules/fastapi-patterns.md`. **Pairwise disjoint ✅** |
| W23 | 1 | S31(30) | `CHANGELOG.md`, `examples/*.py`, `ci.yml`, `tests/test_packaging.py` — shares `test_packaging.py` with S28, so it follows W22 |
| W24 | 1 | S32(31) | no edits; verification + the annotated tag |

**Free-floating lane.** **S30**'s boundary (`CLAUDE.md`, `.claude/rules/fastapi-patterns.md`) is
disjoint from **all 31** other stories. It may be developed in any wave from W0 onward as a spare
lane, provided its merge is held to step-order position 29 (C-3) and OQ8 has been answered.

**Honest summary:** 4 waves carry real concurrency (W9, W10, W13, W15, W22 — five), peak width 3, and
**21 of 25 waves are single-lane.** That is a property of the work, not of this plan: the spec never
claims parallelism, its rollback model is one revertible commit per step, and Phases 0, 6 and 7 are
whole-tree gates by design. Nothing here is a dependency invented by the breakdown.

---

## 8. Serialization points — files that forbid concurrency

Named explicitly, as required. Two concurrent stories may never share any of these.

| File | Stories in scope | Why it serialises |
|---|---|---|
| **`pyproject.toml`** | S3 S5 S7 S24 S25 S26 S27 S28 S31 — **and every story via the ratchet** | Content edits (deps, mypy, flake8, version) *plus* R28's per-step `fail_under` raise. The ratchet half is neutralised by **C-1**; the content half is not, and keeps these nine strictly serial. |
| **`.github/workflows/ci.yml`** | S4 S23 S25 S26 S27 S31 | Every gate adds a named required step to one file. |
| **`tests/conftest.py`** | S1 S10 S11 S15 S16 S17 (6 doubles) | Neutralised by **C-2** — doubles move to `tests/fixtures/<name>.py`; without it, Phases 2 and 3 collapse to one lane. |
| **`helpers/internal/request_helper.py`** | S7 S13 S14 S15 S18 S19 S20 S22 | The transport hub — eight stories. The single biggest constraint on Phase 3 width. |
| **`helpers/internal/filters_helper.py`** | S5 S10 S13 S17 S22 | Two unrelated concerns in one module: the content-type filters (S5, S13, S22) and `get_ssl_config` (S10, S17). This is why S10 ∦ S13 in W9/W10. |
| **`utils/http_file_config.py`** | S14 S15 S18 S19 S20 | |
| **`utils/constants.py`** | S7 S15 S16 S20 | Four unrelated constant edits in one file — the reason S16 ∦ S15 and S20 ∦ S16. |
| **`helpers/internal/base.py`** | S7 S8 S16 S19 | |
| **`logic/http_client.py`** | S5 S7 S13 S15 S19 S21 | |
| **`logic/__init__.py`** | S6 S8 S22 | The protocol registry — renamed, typed, then given its real SOAP entry. |
| **`utils/envelope.py` · `status_map.py` · `redaction.py`** | S7 (creates all three) · S21 (envelope) · S22 (status_map) | The contract modules. Created once by S7 and touched by exactly one later story each — deliberately narrow. |
| **`tests/test_packaging.py`** | S4 S26 S28 S31 | Puts S31 after S28 rather than beside it. |
| **`tests/test_docs.py`** | S24 S29 | Serial across phases; no concurrency lost. |

---

## 9. Human gates on the critical path

| Gate | Story | When it must be answered | If unanswered / declined |
|---|---|---|---|
| **OQ9** — ratify `logic/*.py` → `logic/*_client.py` | **S6** (Step 4.5) | **Before implementation starts** — the rename is in Phase 0 | S6 is cancelled, S25 absorbs a justified `# noqa: A005`, and **every downstream path in this breakdown reverts** — re-issue required |
| **OQ12** — keep-with-facade vs replace vs vendor | **S23** (Step 22) | **At ticket creation (1g)** — it sets S23's file boundary | KEEP → S23 stays docs+CI. REPLACE/VENDOR → **re-plan** (§11-E2). S16's facade proceeds regardless |
| **OQ4** — the version reset is still a safe one-way door | **S28** (Step 27) | Immediately before S28; re-verify the PyPI 404 and log it | R5's reset becomes impossible; the version must go forward and R8's "breaking is free" argument changes shape |
| **OQ7** — the LICENSE copyright line | **S28**, read by **S29** | Before W22 (both lanes read the answer) | S28 blocks; an agent cannot make this call |
| **OQ8** — approval to edit `CLAUDE.md` + `.claude/rules/fastapi-patterns.md` | **S30** (Step 29) | **ANSWERED — APPROVED 2026-08-16** *(was: before W22)* | *Contingency did not fire.* MG7 is recorded **implemented** (commit `7f87275`), **not** "rejected by human" |
| **OQ3** — `requires-python` floor and the matrix ceiling | S3 / S4 | Before W2 | Defaults to the spec's recommendation (`>=3.10`, maintained list) |
| OQ1 · OQ2 · OQ5 · OQ6 · OQ10 · OQ11 | ratification only | any time before S32 | Blocks nothing; the spec records the recommendation for each |

---

## 10. Traceability — every acceptance criterion → ≥ 1 story

AC indices are the checkbox order within each requirement. **258 criteria, 258 mapped, 0 gaps.**

| Req | ACs | Mapping | green-at (where later than the owning story) |
|---|---|---|---|
| R1 | 11 | AC1–9,11 → **S4** · AC10 → **S32** | AC4 fully green at S26/S27 (mypy + coverage steps) |
| R2 | 8 | all → **S1** | |
| R3 | 7 | AC2 → **S2** · AC1,3,4,5,6,7 → **S5** | |
| R4 | 8 | AC1,2,3,4,6,7,8 → **S3** · **AC5 (`py.typed`) → S26** (FI-13) | AC6's "justified in the CHANGELOG" → S31 |
| R5 | 7 | AC1,2,3,5,6 → **S28** · AC4 (tag) → **S32** · AC7 (README) → **S29** | |
| R6 | 7 | AC1,3,4,5,6 → **S2** · AC2 (`pytz`) → **S7** · AC7 (README) → **S29** | |
| R7 | 7 | AC1 → **S16** (tests exist) + **S23** (run in CI) · AC7 → **S16** · AC2,3,4,5,6 → **S23** | |
| R8 | 12 | AC1,3,4,5,6,7,8,9,10,11 → **S7** · AC2 → **S9** · AC12 → **S29** | AC2 green at **S22** (SOAP is the last protocol) |
| R9 | 6 | all → **S7** | |
| R10 | 9 | AC1,2,4,5,6,7,8,9 → **S7** · AC3 → **S8** | AC3's SOAP row green at **S22** |
| R11 | 7 | AC1,2,3,4,6,7 → **S8** · AC5 → **S22** | |
| R12 | 10 | all → **S13** | |
| R13 | 6 | all → **S13** | |
| R14 | 7 | all → **S15** | AC4's SOAP read path green at **S22** |
| R15 | 9 | AC1,2,3,4,5,6,7,9 → **S10** · **AC8 → S19** · AC5 doc half → **S29** | |
| R16 | 6 | AC1–5 → **S11** · AC6 → **S29** | |
| R17 | 9 | AC1,2,3,4,5,6,8,9 → **S12** · **AC7 → S19** | |
| R18 | 9 | AC1–8 → **S22** · AC9 → **S29** | |
| R19 | 7 | all → **S22** | |
| R20 | 5 | all → **S14** | |
| R21 | 6 | AC1,2,5 → **S19** · **AC6 → S15** (FI-16) · AC3,4 → **S29** | |
| R22 | 6 | all → **S18** | |
| R23 | 6 | all → **S17** | |
| R24 | 12 | all → **S16** | |
| R25 | 7 | AC1,2,3,4,5 → **S20** · AC7 export half → **S20**, doc half → **S29** · AC6 → **S29** | |
| R26 | 5 | all → **S21** | |
| R27 | 8 | AC1 → **S1** · **AC4 performed at S6, verified at S25** · AC2,3,7,8 → **S25** · AC5 → **S26** · AC6 → **S22** + **S26** | |
| R28 | 8 | AC1,2 → **S1** · AC3 → **S1** (floor) + every story (C-1) + **S4** (CI check) + **S27** (terminal) · AC7 job → **S4** · AC4,5,6,8 → **S27** | |
| R29 | 11 | all → **S29** | |
| R30 | 9 | AC1,2,3,4,5,7,8,9 → **S24** · AC6 → **S21** + **S24** | |
| R31 | 4 | AC1 **or** AC2 (either/or) → **S28** · AC3 → **S28** · AC4 → **S31** | |
| R32 | 4 | all → **S30** | |
| R33 | 6 | all → **S31** | |
| R34 | 4 | AC1,2,3 → **S28** · AC4 → **S29** | |
| R35 | 5 | all → **S31** | |

**Part B invariants (each is a test, per the spec).** E1 → S7 *(green at S22)* · E2, E3, E4, E5, E6,
E7, E8, E9, E10 → **S7** · E11 → **S7** *(all five protocols green at S22)*.

**Scope-creep audit — every story traces to ≥ 1 criterion.** The two stories most likely to look like
creep, checked explicitly: **S9** maps to R8-AC2 and R28-AC8 (it *is* the spec's Step 7); **S6** maps
to R27-AC4 (it *is* Step 4.5, moved there by FI-15). **S32** maps to R1-AC10, R5-AC4 and the R27
five-item PR gate. **Count: 0.**

---

## 11. Deferrals to the spec, observations, and escalations

**Where I deferred to the spec's step sequence over my own decomposition**

- **D1 — S5 edits `logic/http.py`, not `http_client.py`.** R3's traceability row names
  `logic/http_client.py`, but the rename is Step 4.5 and R3 lands at Step 4. The spec resolves this
  itself (*"still named `logic/http.py` when R3 lands at Step 4"*). Sequence wins; the boundary uses
  the pre-rename path and S6 fixes the imports.
- **D2 — S1 configures coverage in `setup.cfg`, not `pyproject.toml`.** R28-AC2 says *"configured in
  `pyproject.toml` in the scaffolding step"*, but `pyproject.toml` does not exist until Step 2. Both
  Steps 1 and 2 are scaffolding, so the criterion is satisfied by S1 → S3 transcription at the same
  value. Sequence wins; S3's DoD carries "the move must not lower the ratchet."
- **D3 — R15-AC8 and R17-AC7 forward-reference Step 17 from Steps 8 and 10.** Both say the verb is
  *"validated against the R21 allowlist"*, but R21 lands at Step 17. The requirement-ordering table
  does **not** list R15/R17 → R21 as a hard dependency, so this is a small internal tension, not a
  contradiction. **Resolved in the sequence's favour:** S10 and S12 raise `ConfigurationError` for an
  absent/invalid verb (their own edge cases require this anyway), and **S19 closes both criteria** by
  routing them through the real allowlist. Both are mapped in §10; coverage is unaffected.
  **Reported to the spec author as Low** — no action needed for this release.
- **D4 — I did not split Steps 5, 27 or 30 into per-requirement stories**, even though each bundles
  2–3 requirements, because their requirements share files (§8) and a split would create concurrent
  stories with overlapping boundaries. The spec's one-commit-per-step model already had this right.

**Observations, not blockers**

- **O1 — the ratchet is the plan's real concurrency limit, and it is mechanical, not stylistic.**
  Quantified in §3-C1: concurrent lanes each raising `fail_under` from a common base make R1's ratchet
  job fail on the second merge. C-1 is the mitigation; if the orchestrator rejects C-1, the plan is
  **32 waves of one lane** and the wave table in §7 collapses accordingly. Nothing else changes.
- **O2 — S27's boundary excludes source.** "Drive coverage to 100" has no natural file boundary. I
  bounded it to `tests/**` + config and made a required source change a **defect loop back to the
  owning story**. Without that rule S27 silently becomes a licence to edit anything four steps from
  the tag.

**Escalations**

- **E-1 — none blocking.** The spec is internally consistent on every load-bearing point I checked;
  the FI table, the requirement-ordering table and the 31-step sequence agree everywhere except D3
  above, which is Low and resolved.
- **E-2 — OQ12 can invalidate S23, and the spec says so.** If the human answers **REPLACE** or
  **VENDOR**, S23 stops being a docs+CI story and becomes source work on
  `circuit_breaker_helper.py` four steps upstream of S27's non-negotiable 100% gate — and OQ12 states
  plainly that *"each of which needs its own scoped steps, which this plan does not currently carry."*
  **That is a re-plan, not a bigger story: return to the Story Planner before Wave 17.** Asking OQ12
  at Stage 1g rather than at Wave 17 is what keeps this cheap.
- **E-3 — the A6 probe can convert S27 into an infrastructure story.** If S10's Step-8 measurement
  plateaus below ~90% branch coverage on `ftp_client.py` without a live server, the premortem of
  record is realised: a containerised FTP/SFTP fixture becomes necessary, and it is not scoped
  anywhere in this plan. **Escalate from S10, before S11 starts** — that is the whole point of
  probing at Step 8 rather than discovering it at Step 26.

---

## 12. Sizing exceptions — stories that exceed M and why they cannot be split

| Story | Size | Why a split is not available |
|---|---|---|
| **S7** (Step 5) | **XL** — 13 files | R8+R9+R10 are *the contract*, and three separate constraints forbid separating them: **FI-3** (the never-empty message is what makes C2's fix not-a-regression, and it needs the exception hierarchy and the envelope in the same commit), **FI-7** (the envelope defect is *at the seam*, so the fix must be too — `request_helper.py:93` and `logic/http_client.py` land with `utils/envelope.py`), and the **`pytz` atomicity rule** (import and declaration must be removed together or the clean-venv job breaks in one direction or the other). Splitting it would ship the envelope contract with one protocol still building its own response shape — exactly the state FI-7 describes. |
| **S16** (Step 14) | **L** — 6 files, 12 criteria | FI-4 bundles H8 and M16; Ruling D makes the facade own the whole breaker state machine *and* the whole retry loop, which cannot be delivered in halves (counting and opening are one operation; the callbacks and the wait are inseparable from the loop). File-bounded and reviewable, so L is accepted rather than split. |
| **S22** (Step 20) | **L** — 8 files, 16 criteria | R19's hardened parse lives *inside* R18's client and both go green only when the protocol round-trips. Genuinely new code in a remediation release — flagged in the spec's own balance sheet. Cannot ship a SOAP client without its Fault handling. |
| **S24** (Step 23) | **XL** — whole tree | **A sanctioned optional split exists** if the orchestrator wants width at W18: **S24a** `utils/*.py` ‖ **S24b** `helpers/**/*.py` ‖ **S24c** `logic/*.py` + `async_gateway.py` + `__init__.py` (three disjoint boundaries), joined by **S24d** `pyproject.toml` (strictness flags) + `tests/test_docs.py`. Merge order a→b→c→d, all at step 23. Take it only if W18 is on the critical path; the join is small but real. |
| **S13, S15, S19** | **L** | Each is one step's worth of a single requirement pair over a fixed file set. S19 in particular is irreducible: R21's defect *is* that five `getattr` sites across five files have no allowlist, so fixing four of them ships a fail-open surface. |

---

## 13. Handoff to Stage 1g

The coverage gate is **PASS**. Each of the 32 stories above is the source for **exactly one ticket** at
Stage TK (`.claude/skills/ticketing-and-traceability/SKILL.md`), status `OPEN`, in
`docs/project/tickets/`, seeded with: the story's *why* traced to its requirement and finding ids, links
to `docs/specs/v1_release_spec.md` (and to `docs/decisions/0001-resilience-library.md` once S23 creates
it), and the story's **declared file scope verbatim** — the boundary is the ticket's contract, not a hint.

Three tickets carry a human gate in their opening body rather than in a comment: **S6/OQ9**,
**S23/OQ12**, **S30/OQ8**; **S28** carries OQ4 and OQ7. Ask OQ9 and OQ12 **now**, at ticket creation —
OQ9 blocks Phase 0 and OQ12 decides whether S23 needs re-planning.

No task tracker is configured for this repository (no `.claude/state/` tracker config present), so the
local store stays authoritative and no mirror is created. If one is configured later, mirror one issue
per story with the §6 edges carried across, via `.claude/skills/task-tracker-sync/SKILL.md`.
