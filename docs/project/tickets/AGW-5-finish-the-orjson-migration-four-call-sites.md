# AGW-5: Finish the `orjson` migration — **four** call sites

- **Status:** DONE
- **Story:** S5 — spec Step 4, size M (`docs/specs/v1_release_stories.md` §4, Phase 0)
- **Spec:** `docs/specs/v1_release_spec.md` — R3-AC1,3,4,5,6,7 (Group A — Runtime dependencies that are declared, imported, and correct)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** none
- **Files (declared scope):** ~`helpers/internal/response_helper.py`, `helpers/internal/filters_helper.py`, `logic/http.py` *(pre-rename)*, `pyproject.toml` · +`tests/helpers/{__init__,test_filters_helper,test_response_helper}.py`

> **Path note (spec deferral D1):** the boundary names `logic/http.py`, **not** `http_client.py` — the
> rename is Step 4.5 (AGW-6) and this story lands at Step 4. Sequence wins.

## Why

R3 requires the runtime dependencies to be declared, imported and correct. `ujson` is still imported
at four call sites while the declared/target serializer is `orjson`, so the package does not import
from a clean install. This story finishes the migration and removes the stray `requests` import.

Closes findings: C1, MG2. Discharges FI-6.

## ⚠ Four call sites, not three

Three *modules*, **four** *call sites*; `filters_helper.py` carries two. Both numbers are used
deliberately and are not interchangeable. **A story that migrates three sites has not finished.**

1. `response_helper.py:14` `ujson.loads` → `orjson.loads` — a test asserts **both** `str` and `bytes` parse.
2. `filters_helper.py:45` `ujson.dumps(form_value)` in `FormData.add_field` → `orjson.dumps(v).decode()` — test asserts the field value is `str` and byte-identical to today's.
3. `filters_helper.py:78` `data = ujson.dumps(data)` → `.decode()` — test asserts `isinstance(filters['data'], str)`.
4. `logic/http.py:30` the `json_serialize` wiring — the default is a **wrapper** (`lambda o: orjson.dumps(o).decode()`), **never bare `orjson.dumps`**. **FI-6:** `ClientSession(json_serialize=)` requires a `str`-returning callable, and this breaks *after* the clean-venv import starts passing, so it looks like the migration succeeded. A test posts a JSON body through the default serializer against the AGW-1 recording handler and asserts the received body.

## Definition of Done

- All four call sites above migrated and individually asserted
- `grep -rn "ujson" async_gateway/` → 0
- `grep -rn "^import requests\|^from requests" async_gateway/` → 0
- a `bytes`-returning caller serializer is rejected at the boundary with `ConfigurationError`; `serialization=json.dumps` keeps working
- the AGW-4 clean-venv job — failing in reality while passing in CI's pre-installed env — now passes against a truly clean venv
- `orjson.loads` on empty bytes raises a `ValueError` subclass, and the handler catches the `ValueError` **base** so the migration does not change which type escapes

## Dependencies

- **blockedBy:** AGW-4
- **blocks:** AGW-6

## Decisions

- **`ConfigurationError` was created, minimally, in `async_gateway/utils/exceptions.py` — one class,
  not a hierarchy.** Nothing suitable existed: the module held only `CustomGlobalException`, whose
  constructor demands a `headline` and an `error_code`, so satisfying the AC with it would have meant
  a stringly-typed `headline='ConfigurationError'` at every future raise site. Six later stories
  (AGW-8, 10, 12, 15, 16, 17, 19, 20, 22) all raise `ConfigurationError` *by type*, and AGW-7/Step 5
  owns the full hierarchy. A single subclass fixing `error_code=400` per the spec's error table
  (`v1_release_spec.md:3103` — `ConfigurationError → 400 CONFIG`) is the smallest thing that satisfies
  the criterion and the thing AGW-7 will build around rather than replace. **This file is outside the
  declared boundary; see the escalation below.**
- **The `bytes` rejection is a construction-time probe, not a call-time wrapper.** The AC says
  *"rejected at the boundary … rather than failing deep inside aiohttp"*, and only a probe achieves
  that: aiohttp does not call `json_serialize` until the first request. `validated_json_serializer`
  calls the serialiser once with `{}` — the cheapest input every JSON serialiser accepts — and checks
  the return type. The cost is one extra invocation of a caller-supplied callable at construction; the
  benefit is that the mistake is reported before anything is dispatched.
- **Nothing is caught around the probe.** A serialiser that raises on `{}` is already broken, and its
  own exception propagates from the constructor — still at the boundary, still loud. Wrapping it would
  have meant a broad `except`, which R27's zero-suppression criterion forbids.
- **The default is a named function, `default_json_serialize`, not the spec's illustrative lambda.**
  Same body; a name makes it importable, documentable, assertable (`request.serialization is
  default_json_serialize`) and legible in a traceback, and `flake8` E731 forbids binding a lambda to a
  name in any case.
- **`requests` was dropped from `[project.dependencies]` outright.** It was imported by zero modules
  (MG2) and only declared pending this step. The clean-venv install below confirms it is gone from the
  resolved set.
- **`orjson` was already correctly declared** by AGW-2 as `orjson>=3.12.0,<4` — a range, not `~=`, not
  `==`, at the R6 version. No change needed; R3-AC2 verified, not re-implemented.

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`2d7bf40`](https://github.com/ajyadav013/asyncio-gateway/commit/2d7bf40), [`f52e8fc`](https://github.com/ajyadav013/asyncio-gateway/commit/f52e8fc); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


**2026-08-16 — implemented, self-verified, committed.**

*Change:* `~async_gateway/helpers/internal/response_helper.py`,
`~async_gateway/helpers/internal/filters_helper.py`, `~async_gateway/logic/http.py`,
`~async_gateway/utils/exceptions.py` *(outside boundary — see escalation)*, `~pyproject.toml`,
`+tests/helpers/{__init__,test_response_helper,test_filters_helper}.py`.

### All four call sites, each individually asserted

| # | Site | Change | Test |
|---|---|---|---|
| 1 | `response_helper.py:14` | `orjson.loads`; `except ValueError` left **untouched** | `str` and `bytes` both decode; four undecodable bodies (incl. empty `str` **and** empty `bytes`) still give `{}`; and `issubclass(orjson.JSONDecodeError, ValueError)` asserted directly, so the mechanism is pinned and not merely relied upon |
| 2 | `filters_helper.py:45` | `orjson.dumps(v).decode()` | the field arrives as `meta={"a":1,"b":"two"}` (asserted via `parse_qs` on the recorded body) and the request keeps `Content-Type: application/x-www-form-urlencoded` |
| 3 | `filters_helper.py:78` | `orjson.dumps(data).decode()` | `isinstance(filters['data'], str)` parametrised over list/int/None; plus the two untouched branches (`str` passthrough, dict → `json=`) so the change is fenced |
| 4 | `logic/http.py:30` | `default_json_serialize` + `validated_json_serializer` | the serialiser is taken **off a constructed `HttpRequest`** and driven against the recording handler, so the wiring is asserted, not just the wrapper |

**Site 2's assertion is on the request encoding, not the field value — and that is the point.**
`FormData.add_field` accepts `bytes` happily; what it then does is drop the text content type, which
forces the *entire request* from `application/x-www-form-urlencoded` to `multipart/form-data`. A test
that only compared the field value would pass while every form request the library makes changed
shape, in a helper called `form_x_www_form_urlencoded_filters`. Read off the loopback server, the
regression is unmissable.

### FI-6 — the trap, demonstrated rather than asserted

`test_bare_orjson_dumps_really_does_break_aiohttp` passes `orjson.dumps` bare to a real
`ClientSession` and captures what a caller would actually get:

```
AttributeError: 'bytes' object has no attribute 'encode'. Did you mean: 'decode'?
  aiohttp/payload.py:949, in JsonPayload.__init__ -> dumps(value).encode(encoding)
```

Raised from inside aiohttp's payload construction, naming neither this library nor the offending
`protocol_info` key, and **only on the first request** — long after the clean-venv import has started
passing and the migration looks done. The test also asserts `not http_server.requests`: nothing
reached the wire, so there is no failed-request envelope to diagnose from either. That is the failure
`validated_json_serializer` converts into
`ConfigurationError: protocol_info["serialization"] must return str, but 'dumps' returned bytes`
at construction.

### Evidence — commands and exit codes

| Check | Result |
|---|---|
| `grep -rn "ujson" async_gateway/` | **1 match**, `async_gateway.py:41`, a docstring — see escalation. Zero in code. |
| `grep -rnE "^import requests\|^from requests" async_gateway/` | **exit 1** (no matches) |
| `pytest` | **27 passed, exit 0** (7 before this story, 20 added) |
| `pytest -p randomly --randomly-seed=N`, N ∈ {1, 20250816, 424242, 99991, 2147483647} | **27 passed × 5, exit 0** — AGW-4's shuffled-order job is green on every committed seed |
| `mypy async_gateway` | `Success: no issues found in 22 source files`, exit 0 |
| `flake8 tests/` | **exit 0, no output** |
| `flake8` on the four touched source files | response_helper 0 (was 0) · filters_helper 1 (was 1) · http.py **9 (was 10)** · exceptions.py 1 (was 1) — **no violation added**, one removed |
| `python -m build` | exit 0, wheel + sdist |
| `twine check` | **PASSED ×2**, exit 0 |

**The load-bearing one — the clean-venv import that AGW-4 left red is now green.** A fresh 3.12 venv,
`pip install --no-cache-dir dist/*.whl`, then from `/tmp` so the source tree cannot shadow it:

```
from async_gateway.async_gateway import request
  -> SUBMODULE IMPORT OK: <function request at 0x1004e5260>     IMPORT EXIT=0
```

The resolved dependency set in that venv contains `orjson==3.12.0` and **no `ujson`, no `requests`** —
so the import is not passing on a leftover, and the `requests` removal is confirmed end to end. The
dev venv used for the suite had `ujson` **uninstalled** before the final runs for the same reason.

### Escalations (both need orchestrator routing)

1. **Boundary exceeded, deliberately and minimally: `async_gateway/utils/exceptions.py`.** The AC
   names `ConfigurationError` as a *type* and no such type existed. The alternatives were a
   stringly-typed `CustomGlobalException(headline='ConfigurationError', …)` that every later story
   would have to unpick, or declaring the type in `logic/http.py`, which is the wrong home and would
   force AGW-6 to move it during a pure rename. The addition is one class, 26 lines, no change to
   `CustomGlobalException`. Flagged rather than assumed.
2. **R3-AC1 (`grep -rn "ujson" async_gateway/` → 0) cannot be closed inside this boundary.** The last
   textual occurrence is `async_gateway/async_gateway.py:41` — `"serialization": ujson.dumps,
   #Optional`, inside `request()`'s `protocol_info` docstring. That file is not in this story's
   declared scope (it is in **AGW-8**'s). It is now also *wrong*, not merely stale: this commit
   changed that default. One-line fix — `"serialization": callable returning str, #Optional`. Left
   untouched; needs assignment. (My own new comments deliberately avoid the literal token so the grep
   isolates exactly this one line.)

## Work Log — fix round (code review: CHANGES REQUESTED)

**2026-08-16 — four reviewer findings fixed in one follow-up commit.** No rebase, no amend:
`2d7bf40` was no longer HEAD. Orchestrator ratified the `ConfigurationError` addition (escalation 1
above) and granted a **scoped boundary extension to `async_gateway/async_gateway.py` for docstring
corrections only** — AGW-8 still owns that file's dispatch logic.

*Change:* `~async_gateway/async_gateway.py` (docstrings only),
`~async_gateway/logic/http_client.py`, `~tests/helpers/test_filters_helper.py`.

| # | Reviewer finding | Fix | Evidence |
|---|---|---|---|
| 1 | **Medium** `async_gateway.py:41` — the stale docstring example is now *wrong*, not merely stale, and it is the last thing keeping R3-AC1 red | the `protocol_info` example describes a `str`-returning callable and names the orjson-wrapper default | `grep -rn "ujson" async_gateway/` → **exit 1, zero matches**. R3-AC1 closed |
| 2 | **Medium** `async_gateway.py` — `validated_json_serializer` makes `protocol_class(...)` at `:104` raise `ConfigurationError` *synchronously*, outside `handle_request`'s try; `request()` documented no `:raises:`, so a new public-API escape path shipped undocumented (`documentation.md` §3) | added `:raises ConfigurationError:` in the file's existing reST style, saying it escapes at construction *before dispatch* rather than landing in the response dict | docstring only — control flow untouched; whether that escape is *correct* stays AGW-8's question |
| 3 | **Low (FI-6's guard)** nothing covered `json_serialize=self.serialization`; deleting the kwarg broke zero tests — a control that cannot fire (C8/H19) | two tests drive a whole `HttpRequest.handle_request()` at the loopback recording server and assert the **bytes on the wire**: the default's compact encoding, and a caller sentinel serialiser's output | see the guard proof below |
| 4 | **Low** `http_client.py:55` — a non-callable `serialization` raised a bare `TypeError`, though R3's AC calls for rejection *at the boundary* and a non-callable is the same class of caller error as a bytes-returning one | `validated_json_serializer` rejects a non-callable with `ConfigurationError` naming the offending value's type; three-case parametrised test (`str`, `None`, `int`) | `test_a_non_callable_serializer_is_rejected_at_the_boundary` |

### Guard proof — fix 3 fails when the kwarg is removed

An untested guard is not a guard, so it was tested by deleting what it guards. Scratch edit removing
`json_serialize=self.serialization` from `handle_request`, then restore:

```
kwarg removed  -> pytest -k session_serialises   2 failed, exit 1
  E  assert b'{"a": 1, "b": "two"}' == b'{"a":1,"b":"two"}'      (default serialiser)
  E  assert b'{"a": 1}'             == b'{"sentinel":"caller"}'  (caller serialiser)
kwarg restored -> pytest -k session_serialises   2 passed, exit 0
```

Both failures are aiohttp silently falling back to `json.dumps` — the misconfigured session FI-6
predicts, invisible to every construction-time assertion in the file because none of them dispatches.
The caller-sentinel case is also why a `bytes`-returning serialiser cannot reach `ClientSession`: the
validated callable is provably the one that encodes the body, and no other is wired in its place.

### Evidence — commands and exit codes

| Check | Result |
|---|---|
| `grep -rn "ujson" async_gateway/` | **exit 1, 0 matches** (was 1) — R3-AC1 now closed |
| `pytest` | **32 passed, exit 0** (27 before this round, 5 added) |
| `mypy async_gateway` | `Success: no issues found in 21 source files`, exit 0 |
| `flake8` on the three touched files | `async_gateway.py` **0**, `test_filters_helper.py` **0**, `http_client.py` **8 — byte-identical finding set to HEAD** (all pre-existing, owned by AGW-25). No violation added |

### Not fixed here — recorded, owned elsewhere

`README.md:37,98,130` still say `ujson` (**AGW-29**) · `ci.yml` action SHA pinning and missing
`timeout-minutes` (Lows carried to the security/DevOps gate) · `test_packaging.py:32` vs
`ci.yml:255-258` duplicated matrix regex (Cosmetic, accepted) · `MANIFEST.in:6-7` (routed to a later
story).

### Out of scope, deliberately

The spec's second R3 edge case — *"serialisation failures surface as a `ConfigurationError`, not a
fabricated status code"* for a runtime `TypeError` from `orjson.dumps` on an unsupported object — is
**not** closed here. It requires changing `handle_request`'s blanket `except Exception` → `999`
(`logic/http.py:65`, pre-existing), which is the exception→envelope boundary owned by **AGW-7/AGW-8**.
Doing it here would pre-empt that design. The *configuration*-time half of the edge case (a serialiser
of the wrong shape) is fully closed.
