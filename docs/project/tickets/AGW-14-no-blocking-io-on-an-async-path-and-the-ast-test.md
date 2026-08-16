# AGW-14: No blocking I/O on an async path + the AST test

- **Status:** OPEN
- **Story:** S14 — spec Step 12, size M (`docs/specs/v1_release_stories.md` §4, Phase 2)
- **Spec:** `docs/specs/v1_release_spec.md` — R20 all (Group I — Async correctness)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; async correctness
- **Decisions:** none
- **Files (declared scope):** ~`helpers/internal/request_helper.py`, `utils/http_file_config.py`, `tests/helpers/test_request_helper.py` · +`tests/test_no_blocking_io.py`

## Why

R20 requires that no blocking I/O sits on an async path — C3 is four synchronous file operations
inside coroutines, which stall the event loop for every concurrent caller. The streaming async body
that replaces `request_helper.py:185` is also what satisfies R14's retry-safe factory at AGW-15, so
the conversion is done once. The AST test is the part that lasts: it fails when a *new* bare `open(`
appears inside an `async def`, so the fix cannot silently regress.

Closes findings: C3.

## Definition of Done

- all four sites converted (`request_helper.py:69`, `:120,126`, `:185` → a streaming async body which also satisfies R14's retry-safe factory, `http_file_config.py:78` → `aiofiles.os.remove`)
- **an AST test over the whole package** asserts no bare `open(` lexically inside any `async def` and **fails when a new one appears**; no sync `os.remove`/`os.path`/`shutil.` inside an `async def`
- **loop fairness as a counting assertion:** 256 mocked chunks vs an `asyncio.sleep(0)` observer under `gather`, asserting **≥ 256** round-trips — deterministic, no clock read, compatible with R28's no-sleep rule
- no new dependency (`aiofiles` is already used correctly ~30 lines away)

## Dependencies

- **blockedBy:** AGW-13
- **blocks:** AGW-15

## Decisions

_None recorded yet._

## Work Log

- **2026-08-17 — landed.** All four sites converted to `aiofiles`; new package-wide AST scan
  (`tests/test_no_blocking_io.py`) and the 256-chunk loop-fairness counting assertion.
  Files: `async_gateway/helpers/internal/request_helper.py`,
  `async_gateway/utils/http_file_config.py`, `tests/helpers/test_request_helper.py`,
  `tests/test_no_blocking_io.py`. Green: 514 passed / 2 xfailed, coverage 92.38 %
  (ratchet 89.88 → 92.37), mypy clean 24 files, flake8 23 (unchanged from base).
- **Five developer iterations, four review rounds.** The conversion itself was correct in
  iteration 1; every later round fixed a *consequence* of it. Findings closed: part
  `Content-Type` regressing to `application/octet-stream` (aiohttp injects that default for an
  `AsyncIterablePayload`, short-circuiting the filename guess the old sync handle went through);
  a bad local upload path being reported as a remote 502 `CONNECT` once the open moved inside
  `failsafe.run`; a vacuous AST scan that passed over zero files; an anti-buffering test that a
  single-read body satisfied; a pathlib rule narrower than its docstring; and a new blocking read
  introduced *by the Content-Type fix* (`mimetypes.init()` stats 9 paths and reads
  `/etc/apache2/mime.types` lazily, on the loop) — fixed by initialising at import.
- **Ruling V** — the missing-file guard was kept in boundary (`aiofiles.open`, restoring the exact
  prior raise site) rather than moved to `logic/http_client.py`'s `validated_upload_config`. That
  is the better long-term home, but it would have created new `ConfigurationError` contract
  surface and deepened AGW-35. Routed to S19, which owns that file.
- **Ruling W** — `'ssl.'` added to the scan's banned prefixes with a single containment allowance
  (`NOT_YET_REWRITTEN`) for `filters_helper.py`/`get_ssl_config`, modelled on Ruling H. Keyed on
  the **triple** `(module, function, call)` after review found a `(module, function)` key muted
  the whole function. A containment test pins the allowance at one entry; **S17 must delete it**.
- **Three tickets opened from this story's findings**, all pre-existing package defects S14
  documents rather than fixes: **AGW-36** (Critical, → S17) blocking TLS context build in
  `get_ssl_config`; **AGW-37** (Critical, owner unassigned) the same in `tls_context_for`, reached
  from `_tls_value` and structurally invisible to a lexical scan; **AGW-38** (High, → S15) the
  sibling streaming-upload path still replaying an exhausted body — measured attempt 2 = 0 bytes
  with HTTP 200.
- **Accepted cost (chunked framing).** A streaming upload body has no known length, so the request
  now goes out `Transfer-Encoding: chunked` instead of with a `Content-Length`. The reviewer
  refuted the "stream and keep a correct `Content-Length`" alternative on evidence (aiohttp fixes
  `AsyncIterablePayload._size = None`; a stat-derived length would be a *lying* Content-Length,
  which is worse). Owner: S29 for the README note. Revisit trigger: a consumer reporting a server
  that rejects chunked uploads.
- **Orchestrator error, recorded.** A `git checkout async_gateway/utils/http_file_config.py` used
  as a mutation-restore shortcut destroyed that file's uncommitted S14 change. S14's own AST guard
  caught it immediately (suite went red on the restored `os.remove`); the change was re-applied and
  diffed line-by-line against `78d97a5` by the reviewer. Backups, not git, restore a mutation.
