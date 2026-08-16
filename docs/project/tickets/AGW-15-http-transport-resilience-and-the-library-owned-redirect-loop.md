# AGW-15: HTTP transport resilience + the library-owned redirect loop

- **Status:** OPEN
- **Story:** S15 — spec Step 13, size **L** (`docs/specs/v1_release_stories.md` §4, Phase 3; sizing exception §12 — one step's worth of a single requirement pair over a fixed file set)
- **Spec:** `docs/specs/v1_release_spec.md` — R14 all (Group E — HTTP protocol correctness) · R21-AC6 (Group J — The caller-controlled capability surface)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; the transport layer
- **Decisions:** none
- **Files (declared scope):** ~`logic/http_client.py`, `helpers/internal/request_helper.py`, `utils/http_file_config.py`, `utils/constants.py`, `tests/logic/test_http_client.py`, `tests/helpers/test_request_helper.py`, `tests/fixtures/http_server.py`

## Why

R14 requires an HTTP transport that pools connections, always has a deadline, and bounds what it
reads — today a session may be constructed without a timeout, an unbounded response can exhaust
memory, and a retried upload replays an already-consumed body. R21-AC6 lands here rather than at
AGW-19 because FI-16 makes the redirect loop and the per-hop scheme check **one fix**: owning the loop
without re-checking each `Location` leaves the SSRF hole open, and asserting the check without owning
the loop asserts something the library does not control.

Closes findings: H9, H10-http, H11, H12, MG4, M21-in-scope. Discharges FI-16.

## Definition of Done

- caller-supplied `session` used and **not closed**; created+closed when absent; two requests on one session reuse a connection (via `on_connection_reuseconn`) and it stays open; a closed session → `ConfigurationError`
- `grep -n "ClientSession("` shows **no** construction without `timeout=`
- per-protocol timeout → `TIMEOUT`/`504` within the deadline, driven by a handler awaiting an `asyncio.Event` released in teardown with `timeout` in tens of ms — *the test waits only on the deadline it asserts, which is the behaviour under test, not the sleep-to-settle pattern R28 forbids*
- `max_response_bytes` **64 MiB** default, per-call override, no disable sentinel; over-cap → `RESPONSE_TOO_LARGE` without allocating the body; enforced on **every** read path (in-memory, streamed download, and SOAP from AGW-22) — `Content-Length` over cap rejected **before** the read, chunked rejected mid-stream *(`request_helper.py:34` excluded: it is inside `fetch_file`, which AGW-20 deletes)*
- **H9:** body from a **factory invoked per attempt**; one failure then one success and attempt 2's body length equals the file's real length (a zero-byte upload is legitimate, so the assertion is on the real length, not "non-zero")
- chunk default 64 KiB
- **FI-16 — the redirect loop and the per-hop check are one fix:** `allow_redirects=False` on the transport, the library follows, bounded by `max_redirects` (10); **every** `Location` resolved and re-checked against `allowed_schemes` **before** the next request, a rejection raising `CONFIG` and naming scheme + hop. Tests: 302→`ftp://` refused with the fixture's **recorded request count staying at 1**, 302→`file://` likewise, a relative `Location` resolved and followed, `max_redirects+1` fails. *A story that surfaces `max_redirects` without the per-hop check, or asserts the check without owning the loop, is rejected.*

## Dependencies

- **blockedBy:** AGW-14
- **blocks:** AGW-16, AGW-17, AGW-18, AGW-21

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
