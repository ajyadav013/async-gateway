# AGW-21: A request tracer whose results belong to one request

- **Status:** OPEN
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

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
