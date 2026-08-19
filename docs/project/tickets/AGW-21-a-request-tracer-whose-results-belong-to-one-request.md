# AGW-21: A request tracer whose results belong to one request

- **Status:** IMPLEMENTED (branch `lane/s21`)
- **Story:** S21 — spec Step 19, size M (`docs/specs/v1_release_stories.md` §4, Phase 3)
- **Spec:** `docs/specs/v1_release_spec.md` — R26 all (Group L — Utilities and observability primitives) · R30-AC6 (Group N — Documentation)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; observability primitives
- **Decisions:** none
- **Files (declared scope):** ~`utils/request_tracer.py`, `logic/http_client.py`, `utils/envelope.py` · +`tests/utils/test_request_tracer.py`

## Why

R26 requires a request tracer whose results belong to one request: today they are stored on the shared
`TraceConfig`, so two concurrent requests through one caller-supplied config interleave each other's
events. `on_connection_reuseconn` stores an absolute timestamp among 13 relative deltas and overwrites
the baseline, which under-reports latency on every keep-alive connection. R30-AC6 annotates all 15
callbacks — 15, not the 14 the audit's prose said.

Closes findings: H17, M11, M20-part.

## Definition of Done

- results live on the per-request trace **`context`**, collected into the envelope at the end — never on the shared `TraceConfig`; **two concurrent requests through one caller-supplied `trace_config` under `gather`** each carry only their own events (no interleaving, no stale `on_request_exception`)
- `on_connection_reuseconn` records a **relative delta** like its 13 siblings and does **not** overwrite the baseline; a reused connection still measures from the original start *(today it under-reports latency on every keep-alive connection and stores an absolute timestamp among 13 relative deltas)*
- `on_request_exception` stores a **string** via AGW-7's `unwrap_cause`, never a live exception; a test asserts `isinstance(..., str)` and no `Authorization` value *(a `ClientResponseError` carries `.request_info.headers`)*
- **all 15** callbacks annotated and each exercised at least once (`request_tracer.py:111-125` — **15**, not the 14 the audit's prose said)
- a foreign `trace_config` must not be assumed to have `results_collector`; `trace_config=[]` → `[]`, not `KeyError`; a failure before `on_request_start` covered

## Dependencies

- **blockedBy:** AGW-15
- **blocks:** AGW-22, AGW-24

## Decisions

- **`results_collector` becomes a view, not storage.** It is public API and is read by
  `logic/http_client.py` and `helpers/internal/request_helper.py`, so deleting it would break
  three consumers and every caller who reads their own tracer after a call. `ResultsCollector`
  is a `MutableMapping` resolving every operation through a per-tracer `ContextVar`, so the
  attribute keeps its documented behaviour while the storage underneath belongs to one request.
- **`ContextVar`, not a parameter thread.** The alternative — passing a per-request mapping down
  through `handle_http_request` — would have required changing `helpers/internal/request_helper.py`,
  outside this story's file boundary. `asyncio.gather` copies the context per task, which is
  precisely the isolation H17 needs, so the scope is bound in `handle_request` (inside the
  coroutine, not `__init__`: the context that matters is the awaiting task's).
- **A redirect chain shares one scope.** Several aiohttp requests, one caller request, last write
  winning — matching what aiohttp did when it owned the loop, and what `record_redirect` measures
  against. The unit isolated is the caller's request, which is the unit H17 is about.

## Work Log

- **Implementation** (`411ca2f`) — `utils/request_tracer.py` rewritten; `logic/http_client.py`
  wired to bind a per-call scope. All 800 pre-existing tests stayed green, which is the evidence
  the `results_collector` contract was preserved rather than merely replaced.
- **Tests** (`tests/utils/test_request_tracer.py`, new; `tests/utils/__init__.py`) — 29 tests.
  Coverage for `request_tracer.py` **74.29% → 100%**, the package's lowest module before this.
- **Mutation proof** — each guard was verified able to fail by mutating *inside* the protected
  code and confirming red, then restoring from a `cp` backup: (1) results back on a shared dict →
  5 red, including the decisive `asyncio.gather` test; (2) `on_connection_reuseconn` overwriting
  the baseline → 2 red; (3) storing the live exception → 2 red; (4) dropping the caller's
  `redact_params` → 1 red; (5) assigning `request_tracer` only on the success path → 4 red.
- **Two defects found while testing, both fixed in scope.** Neither is in the ticket's original
  finding list; both were invisible until the failure path reported a trace at all.
  - *A failed envelope reported `request_tracer: []`.* The key was assigned in
    `_copy_into_envelope`, reachable only where a response came back — so the one event a failed
    call uniquely produces, `on_request_exception`, was recorded correctly by the tracer and then
    discarded by the envelope. This is invariant E11's shape ("`ok=False` never costs the caller
    the body") applied to the trace. Now assigned when the scope is bound.
  - *An E9 leak in the trace's exception message.* With the failure path reporting a trace, the
    R10 redaction tests in `tests/test_envelope.py` went red: the tracer called `unwrap_cause`
    with the built-in names only, so `api_key` was masked and a caller's declared `session_id`
    was not. `aiohttp`'s `InvalidUrlClientError` stringifies to the whole URL including its query
    string, so the unmasked parameter landed on the envelope verbatim. `request_tracer()` now
    takes `redact_params`, threaded from `validated_trace_config`.

## Known limitation (recorded, not fixed — outside this story's boundary)

`on_response_chunk_received` is registered and correct but never fires on the library's own read
path. Measured on aiohttp 3.14.3: `resp.text()` fires it once, `resp.content.iter_chunked()` not
at all — and `read_response` uses the latter (the capped reader S15 owns). Changing that means
editing `helpers/internal/request_helper.py`, outside the declared file scope. The callback is
therefore exercised directly rather than through an envelope, and
`test_the_chunk_received_callback_fires_when_aiohttp_fires_it` states why.
