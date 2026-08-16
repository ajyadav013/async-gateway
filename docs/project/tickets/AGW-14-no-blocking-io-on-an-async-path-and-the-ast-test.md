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

_Empty — opened at stage 1g, before implementation._
