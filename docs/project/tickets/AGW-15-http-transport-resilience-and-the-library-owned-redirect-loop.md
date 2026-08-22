# AGW-15: HTTP transport resilience + the library-owned redirect loop

- **Status:** DONE
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

- **The cap primitives live in `utils/http_file_config.py`, not beside their busiest caller.**
  `guard_declared_length`, `iter_capped` and `response_too_large` are needed by both HTTP read
  paths — `helpers/internal/request_helper.py` and `utils/http_file_config.download_file_from_url`.
  `helpers/` may import `utils/`; the reverse is a layering inversion. One implementation means the
  two paths cannot disagree about what "too large" is, or word the refusal differently. The module
  name is a poor fit and is noted rather than fixed: AGW-20 restructures that file.
- **`headers`, `cookies` and `auth` moved from the session constructor to the request call.** Two
  reasons, both load-bearing. A caller-supplied session would otherwise have all three silently
  dropped (they are session-level in aiohttp). And the owned redirect loop must be able to
  *withhold* them on a hop that crosses an origin — a session default cannot be suppressed on one
  request, so an `auth` left on the session would follow a hostile redirect no matter what the loop
  did about the headers. `serialization` is the one that cannot follow: `json_serialize` exists only
  on the session, so a caller who supplies a session supplies its serialiser too. Documented on
  `HttpRequest.handle_request`.
- **Exceeding `max_redirects` raises `TransportError` carrying the last status**, rather than
  returning the redirect response. A 302 is below `HTTP_ERROR_STATUS`, so returning it would have
  produced `ok=True` — the criterion asks for `ok=False` with the last status in `status_code`, and
  a status override on the exception is what gives both.
- **The redirect loop reproduces aiohttp's verb/body rule** (301/302 on a POST and 303 on anything
  but HEAD become a bodyless GET; 307/308 repeat). Taking the loop over is a means to the per-hop
  scheme check; changing what a 303 does would be a behaviour change nothing asked for.
- **Cross-origin credential stripping was added with the loop.** aiohttp drops `Authorization`,
  `Cookie` and `Proxy-Authorization` when a redirect changes origin. Owning the loop without
  reproducing that would have traded an SSRF hole for a credential leak.
- **Accepted cost: the per-invocation deadline multiplies the caller's `timeout` by
  `allowed_retries + 1`.** `make_http_request` takes its deadline inside the function `failsafe.run`
  re-invokes, so each retried attempt starts the caller's budget over. Measured: `timeout=0.15` with
  2 retries spent 0.471s across 6 requests. This is **pre-existing base behaviour**, not something
  S15 introduced — at base `919416b` aiohttp was handed a fresh `ClientTimeout(total=)` per attempt
  too. Not changed here, deliberately: moving the deadline outside `failsafe.run` would make a
  retry useless whenever the first attempt spent the whole budget, which is a behaviour change
  nothing asked for on an axis this story was not opened to touch.
  **The multiplier is caller-reachable today, not latent.** An earlier wording said it stayed dormant
  until a story raised `allowed_retries` above `0`; that was wrong. `allowed_retries` is a
  *caller-supplied* key —
  `protocol_info['circuit_breaker_config']['retry_config']['allowed_retries']` — so any caller
  reaches the multiplier on the call they are writing now, without any story shipping first. The
  library default of `0` sets the floor, not the ceiling.
  **And the retry loop amplifies refusals, not only slow calls.** A refusal raised *by this library*
  is retried like a transport fault, because `abortable_exceptions` defaults to none and
  `retriable_exceptions` to everything. Measured against the loopback fixture through the public
  `request()`: a 302 to `ftp://` — a hostile hop the per-hop scheme check refuses — costs 1 request
  with the default retry config and **4** with `allowed_retries=3`; an over-cap body is the same
  shape, 1 request against 4. Each refusal is correct and each is re-issued, so the guardrail's own
  rejection becomes the request amplifier.
  **Owner: S16**, which owns `circuit_breaker_helper.get_retry_policy` and therefore the retry-policy
  defaults. **Revisit trigger: S16 itself** — this is not waiting on a future condition.
  Two things are explicitly handed to it: the deadline's placement relative to `failsafe.run`, and
  **`abortable_exceptions` defaulting to none**, which is what makes a `ConfigurationError` or a
  `ResponseTooLargeError` this library raised on purpose retriable at all. Neither is fixable inside
  S15's declared file boundary — both live in `helpers/internal/circuit_breaker_helper.py`, which
  this story does not touch.
- **The envelope keeps reporting `[]` for a caller-supplied session, even though the loop now
  writes into that session's tracers.** Round 3's H2 fix makes `record_redirect` reach a supplied
  session's collectors (see the round-3 log); the *envelope*'s contract is left alone. Those
  collector dicts belong to an object that outlives the call, so a session reused across five calls
  would have all five envelopes aliasing one mutating dict and the first envelope would silently
  report the fifth call's timings. The defect being fixed was the collector being unreachable, not
  the envelope being empty, and widening the envelope is a separate decision with its own aliasing
  cost. The caller already holds the tracer they attached.
- **`handle_multipart_response` is capped too**, though the criterion names only the in-memory read
  and the streamed download. It is the third read path in this module and it accumulates every part
  in an in-memory list, so leaving it out would have made `max_response_bytes` a cap on two of the
  three ways the module reads a body. SOAP's read path is untouched — it is AGW-22's.

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`c5026c9`](https://github.com/ajyadav013/asyncio-gateway/commit/c5026c9); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


### 2026-08-17 — implementation (S15)

**Files changed** (`git diff --name-only`):

```
async_gateway/helpers/internal/request_helper.py
async_gateway/logic/http_client.py
async_gateway/utils/constants.py
async_gateway/utils/http_file_config.py
pyproject.toml
tests/fixtures/http_server.py
tests/helpers/test_request_helper.py
tests/logic/test_http_client.py
```

**What changed, per Definition-of-Done item.**

- *Caller-supplied session.* `protocol_info['session']` is validated by
  `http_client.validated_session` and used without being closed; absent, `handle_request` builds one
  and closes it. `_exchange` is the shared body so the two paths cannot drift. Reuse is asserted
  through the tracer's `on_connection_reuseconn`, with the no-session case asserted as its negative
  control; a closed session and a non-session are both `ConfigurationError`, raised in the
  constructor and therefore before anything is dispatched.
- *Every session carries a deadline.* `grep -n "ClientSession(" async_gateway/` shows three
  constructions, all with `timeout=`. Pinned by an AST test
  (`test_no_session_in_the_package_is_built_without_a_deadline`) with its own
  can-this-scan-fail companion, on the S14 precedent.
- *The deadline covers the transfer.* `timeout` is passed on the **request**, not only the session,
  so a supplied session's own deadline cannot outrank the call's. Driven by the fixture's new
  `asyncio.Event`-gated handler with the library timeout at 50 ms.
- *`max_response_bytes`.* Default 64 MiB, per-call override, no disable sentinel (`0`, `-1`, `None`,
  `'64'` and `True` are all rejected). Enforced pre-read on `Content-Length` and mid-stream on all
  three read paths.
- *H9 / AGW-38.* `make_http_filters_with_stream_file_upload` now builds its body inside an
  `async def attempt()` closure passed to `failsafe.run`, with the `aiofiles.open` guard outside it
  — the shape the non-streaming sibling already had. Measured on the wire across two attempts with
  the server answering 500 then 200: **before, attempt 2 sent 0 bytes and the server answered 200;
  after, both attempts send 1024 bytes** (`len(UPLOAD_BYTES)`), and the final result is the 200.
  The previously-uncovered dispatch branch is covered, and the missing-file guard is now
  parametrised over both branches × three errnos.
- *Chunk default.* `CHUNK_SIZE_CONSTANT` 1024 → 65536, pinned as a number and asserted as the size
  the download actually asks its stream for when unset.
- *FI-16.* The transport is called with `allow_redirects=False` and `make_http_request` follows;
  every `Location` goes through `redirect_target`, which resolves it against the current URL and
  re-checks the scheme **before** the next request. R21-AC6 is asserted on the fixture's recorded
  request count staying at 1 for `ftp://` and `file://`, plus a narrowed-allowlist `http://`
  downgrade; a relative `Location` resolves and is followed; `max_redirects + 1` fails with the last
  status; `max_redirects` exactly is fine; `allow_redirects=False` returns the 302 unfollowed.

**Fixture (`tests/fixtures/http_server.py`).** Added per-method specs, `respond_in_sequence`
(last spec repeats), `gate()` (released by `close()` so a deadline test always tears down), a lying
`content_length`, and `chunks` for a chunked body with no `Content-Length`. Fixed its three
reviewer Lows: `path`/`raw_query` are now the bytes as sent (with `query` documented as the decoded
convenience view), the body is accumulated and recorded from a `finally` so an aborted upload is
recorded with `body_complete` False, and `ResponseSpec.headers` is a sequence of pairs (a mapping is
still accepted) so a repeated header is expressible.

**Mutation proofs.** 19 mutations applied one at a time, each confirmed to have landed by md5 and
each reverted to a matching md5: per-hop scheme check removed; `allow_redirects` back to True;
declared-length guard removed; mid-stream cap neutered; streaming body built once outside the retry;
cross-origin header stripping removed; cross-origin `auth` kept; per-request `timeout`/`headers`/
`cookies`/`auth` each dropped; closed-session guard removed; cap validator neutered; multipart cap
removed; `max_redirects` bound removed; 303 downgrade removed; fixture recording only complete
bodies; fixture recording the decoded path; chunk default back to 1024. **All 19 killed.** Two
survived on the first pass and both were real defects in the *tests*: the multipart cap test was
being satisfied by the `Content-Length` guard rather than by the multipart counter (fixed by serving
the multipart body chunked), and one mutation had been written so it did not actually change
behaviour (a `return` before a `finally`).

**Gate.** `pytest` 599 passed, 2 xfailed (the two SOAP xfails, unchanged) · coverage
**95.223179%**, identical across all five committed seeds · `mypy async_gateway` clean, 24 files ·
`flake8 async_gateway tests` = 23, the unchanged pre-existing set. `fail_under` raised
92.37 → **95.22** (truncated) as the last edit.

**Closes:** H9 (both paths), H10-http, H11, H12, MG4, M21-in-scope, FI-16, R21-AC6 — and
**AGW-38**, whose fix is this story's H9 criterion. AGW-38's own ticket is outside this story's file
boundary and has been left for the orchestrator to close.

---

## Work log — review round 1 (four blockers, same file boundary)

Mutation testing cleared the story's own guards; the code review found four defects mutation
testing structurally cannot see, because each is a **silently unimplemented half of a feature the
docstrings claim in full** — no mutation of the code that exists can kill a test for behaviour that
was never written.

**C1 — a supplied session leaked `Authorization` across an origin (regression vs raw aiohttp).**
`without_credentials` strips only the *per-request* headers, and aiohttp merges a session's
`_default_headers` and `auth` into every request underneath them — so the `protocol_info['session']`
feature added in this same commit sent `ClientSession(headers={'Authorization': ...})` to whatever
host a hostile `Location` named, where raw aiohttp strips it. Fixed at the boundary rather than in
the loop, because the loop provably cannot withhold a session default: `validated_session` now
refuses a session carrying any of `CREDENTIAL_HEADERS` in its public `session.headers`, or a
session-level `session.auth`, with a `ConfigurationError` naming `protocol_info['headers']` and the
`auth` argument as the routes that *are* stripped on a cross-origin hop. Session-level cookies leak
identically under raw aiohttp and are left alone: that is parity, not a regression.
New: three refusal tests (each credential header, plus casing; the `auth` spelling) and the
cross-origin redirect test **driven through `session=`** — the shape no redirect test had, which is
why the leak was invisible to a suite that covered the same hop without one.

**C2 — a redirect on an upload re-sent a consumed body: truncated upload reported `ok=True`.**
`after_redirect` returned the previous hop's `body_filters` verbatim on 307/308, and both upload
paths put a one-shot object there. aiohttp *refuses* this (`Cannot follow redirect with a consumed
request body`); owning the loop deleted the refusal and turned a loud error into silent data loss.
Fixed with the factory, not the refusal: `make_http_request`'s third parameter is now a
`BodyFactory` (`Callable[[], Awaitable[Dict]]`) awaited **once per hop**, and — since `failsafe.run`
re-invokes `make_http_request` — once per attempt. All three filter methods supply one, which
supersedes and deletes the two per-attempt `attempt()` closures: one mechanism now covers both the
retry and the hop on every path, where the closure covered only the retry on two of them.
Measured on the wire, 960-byte file, 307 → 200:

| path | before (hop1, hop2) | after |
|---|---|---|
| streaming (`file_upload` generator) | `[960, 0]` | `[960, 960]` |
| multipart (`build_upload_form`) | `[1147, 187]` (187 = boundary, no file) | `[1147, 1147]` |
| urlencoded (`FormData`, no-file path) | `[11, 11]` | `[11, 11]` |

The urlencoded row was **green before the fix** — a urlencoded `FormData` re-encodes where a
multipart one does not — and is stated as such in its test docstring rather than claimed as a fix.
It is pinned because the same factory now feeds it.

**M3 — three `protocol_info` keys shipped unvalidated, two lines below two validated siblings.**
`allow_redirects`, `max_redirects` and `allowed_schemes` were bare `.get()`s.
`max_redirects='three'` escaped `request()` **un-enveloped** as `TypeError: '>=' not supported
between instances of 'int' and 'str'`, and only when a redirect happened to arrive;
`allowed_schemes='https'` silently became `frozenset({'h','t','p','s'})`; `allow_redirects='no'`
followed redirects. Added `validated_allow_redirects` (strict `bool` — no truthiness, which would
invert the caller's intent), `validated_max_redirects` (non-negative `int`, `bool` rejected as it is
for the response cap; **zero is legal** and means "follow none"), and `validated_allowed_schemes`
(bare `str` rejected first and by name, non-`str` members rejected, empty rejected, names lower-cased
once so an `{'HTTPS'}` allowlist admits what it names). All follow the existing `validated_*` style
and error contract, and all refuse at construction — asserted on the recorded request count staying
at zero.

**M4 — the `after_redirect` table proved the rule only with a replayable body.** This is the
assertion-one-field-short that let C2 through: `307-repeats` passed `{'data': b'x'}`, and bytes
replay where the generator and `FormData` the library actually sends do not. The input is now its
own explicit column — it was derived from the expected *output*
(`dict(expected_body) or {'data': b'x'}`), so every to-GET row read as though it stated an input and
was in fact handed `{'data': b'x'}` by the `or`. Three rows added over the two real one-shot shapes
(`307-repeats-a-form`, `308-repeats-a-stream`, `303-drops-a-form`).

**L7 — `guard_declared_length` judged a repeated `Content-Length` on the first line.** It now reads
every line, ignores what will not parse instead of returning on it, and judges the **largest**
declared value. Two lines is a framing error and the shape a smuggling attempt takes; arrival order
let an over-cap declaration through behind a small one, past the one check whose whole purpose is to
refuse before a byte is read.

**L5 — not done, and why.** `ConfigurationError` / `ResponseTooLargeError` are indeed raised inside
`failsafe.run` and are retriable; the remedy is `abortable_exceptions` on `get_retry_policy`'s
defaults, and `get_retry_policy` lives in `helpers/internal/circuit_breaker_helper.py`, **outside
this story's boundary** (S16/S19 own it). Left for the orchestrator to route. Harmless today only
because `Failsafe(retry_policy=None)` defaults to `allowed_retries=0`.

**L6.** `protocol_info['serialization']` was validated and then silently dropped whenever a session
was supplied, because `json_serialize` is session-only. `validated_serialization` now refuses the
combination and names the working route (`ClientSession(json_serialize=...)`), so the constructor
no longer accepts a key it cannot honour. `__init__` reads `session` before `serialization` for
this reason.

**L8 — deliberately not done.** The README's missing `protocol_info` keys are owned by the S29
README rewrite; `README.md` is outside this boundary.

**Mutation proofs (round 1).** 10 further mutations, each md5-confirmed as landed and each reverted
to a matching md5: session credential-header refusal removed; session `auth` refusal removed;
per-hop `await build_body()` reverted to carrying `body_filters` forward; `validated_max_redirects`
bypassed; `validated_allow_redirects` bypassed; bare-`str` `allowed_schemes` accepted; allowlist
lower-casing removed; `serialization`+`session` refusal removed; repeated `Content-Length` judged on
the first line (`continue` → `return`); largest-of-two replaced by first-of-two. **All 10 killed.**

**Gate (round 1).** `pytest` **627 passed, 2 xfailed** (the two SOAP xfails, unchanged; no new
xfails) · coverage **95.427286%**, identical across all five committed seeds · `mypy async_gateway`
clean, 24 files · `flake8 async_gateway tests` = **23**, the unchanged pre-existing set.
`fail_under` raised 95.22 → **95.42** (truncated). Coverage rose; nothing was lowered.

---

## Work log — review round 2 (what `allow_redirects=False` quietly cost)

Round 1 confirmed the four blockers fixed and the `BodyFactory` refactor's blast radius clean. Round
2 found the *sibling* change: taking the redirect loop off `aiohttp` inherited two guarantees
`aiohttp` used to provide, and neither was reproduced.

**H1 — the caller's deadline was applied per *hop*, so the loop multiplied it by up to
`max_redirects + 1`.** At base `919416b` the transport was called as `request_obj(url, **filters)`
with no `allow_redirects`, so **aiohttp** owned the loop and its `ClientTimeout(total=)` bounded the
whole chain. Owning the loop and passing `timeout=timeout` unchanged inside `while True:` handed
every hop a fresh `total`. Measured end to end, 6 slow hops + a slow final answer (7 × 0.10s) against
`timeout=0.15`:

| | ok | code | status | requests | elapsed |
|---|---|---|---|---|---|
| before | `True` | — | 200 | 7 | **0.72s** |
| after | `False` | `TIMEOUT` | 504 | 2 | **0.16s** |

A 4.8× overrun on this chain; 11× on the default `max_redirects=10`. It failed R14's acceptance
criterion *"a caller-set `timeout` is honoured on every protocol"* and is the exact hazard R14's user
story names. Nothing covered timeout × redirect, which is why it survived round 1.

New `hop_deadline(timeout, deadline, *, url, redact_params, hop)` in `request_helper.py` returns the
hop's `ClientTimeout` out of what the chain has left, raising `asyncio.TimeoutError` (already
classified `TIMEOUT`/504, so no new exception surface) when the budget is gone. The deadline is taken
**per invocation** of `make_http_request`, so each `failsafe.run` attempt still gets a fresh budget —
the old semantics exactly. `total=None` is passed through untouched; `connect` / `sock_connect` /
`sock_read` are per-connection and are not the chain's to spend, so they carry through.

**M2 — a caller-supplied `session` silently voided `trace_config`, and `request_tracer` came back
empty.** `handle_request`'s `if self.session is not None:` branch never passed `trace_configs`, yet
`_copy_into_envelope` still filled the envelope from `self.trace_config`: an owned session produced
five collector keys, a supplied one produced zero. The identical shape to the `serialization` +
`session` no-op fixed in round 1, and now refused the identical way — new `validated_trace_config`
names `ClientSession(trace_configs=…)` as the supported route. With a supplied session and no
`trace_config`, no tracer is built at all and the envelope reports `[]`, which truthfully says
"tracing is off" rather than offering a collector nothing was ever wired to.

**M3 — `is_redirect` / `on_request_redirect` were permanently dead. Fixed *in boundary*; no referral
needed.** `aiohttp`'s `on_request_redirect` cannot fire once the transport never redirects, so
`request_tracer[*]['is_redirect']` was `False` on every chain this library followed — a top-level
`GatewayResponse` key consumers branch on, and a direct violation of `constants.py:48` (*"owning it
ourselves must preserve the semantics, not invent new ones"*). `utils/request_tracer.py` was **not
touched**: `record_redirect` writes into the `results_collector` dicts, which `http_client` now
derives once (`self.trace_collectors`) and passes down, so the collectors the loop writes are
provably the same objects the envelope reports.

One design point found by the first RED run: the event must be written **once, after the last hop**,
not as each hop is followed. `aiohttp` ran its loop inside a single traced request, so
`on_request_start` fired once for the chain; this loop issues each hop as its own request, so that
callback fires per hop and resets `is_redirect` to `False` again — a flag written at the hop is
erased by the request the hop leads to. The elapsed value is measured from the start of the chain,
which is what `aiohttp` measured. Measured after: followed 302 → `is_redirect=True`,
`on_request_redirect=0.0013`; no redirect → `is_redirect=False`, key absent.

**L4.** `verb = request_type.strip().upper()` now has table rows: `302-untidy-post` (`' post '`) and
`303-head-keeps-its-verb` (the one row where `to_get` is False for a 303).

**C5.** `ONE_SHOT_STREAM` no longer exists as a module-scope async generator held for the session;
`test_a_308_repeats_a_one_shot_stream_body` builds it and `aclose()`s it in a `finally`.

**Fixture.** `ResponseSpec.delay` / `respond(delay=…)` — "answers late", distinct from `gate`'s
"never answers". A chain of *slow* hops is the only shape that can tell a budget spent once over the
exchange from one handed afresh to every hop.

**Mutation proofs (round 2).** 8 mutations, each md5-confirmed as landed and each reverted to a
matching md5: `hop_deadline` returning the full budget to every hop; `record_redirect` made a no-op;
`record_redirect` firing with no hop followed; `record_redirect` moved *before* the scheme check (the
realistic alternative placement — killed by the refused-hop test, which is otherwise its only
guard); the `trace_config` + `session` refusal removed; that refusal widened to reject every supplied
session; `.strip().upper()` dropped; the `verb != 'HEAD'` exemption dropped. **All 8 killed, each by
the test written for it.**

**Not done, and why.** `logic/http_client.py`'s `CircuitOpen` arm of `_exchange` is the one
uncovered statement left in that file — no HTTP test drives the breaker open, and
`circuit_breaker_helper.py` is outside this boundary (S16 owns that seam). Pre-existing, not
introduced here. Noted for the orchestrator alongside L5.

**Gate (round 2).** `pytest` **639 passed, 2 xfailed** (the two SOAP xfails, unchanged; no new
xfails) · coverage **95.557174%**, identical across all five committed seeds · `mypy async_gateway`
clean, 24 files · `flake8 async_gateway tests` = **23**, the unchanged pre-existing set.
`fail_under` raised 95.42 → **95.55** (truncated). Coverage rose; nothing was lowered.

---

## Work log — review round 3 (round 2's fixes, finished)

Round 2 fixed the two guarantees `allow_redirects=False` silently cost. Round 3 found that two of
those fixes were themselves partial, and that the last unvalidated `protocol_info` key was the one
round 2 had touched. Every finding below was **measured against raw `aiohttp` on the identical
shape** before anything was written, and re-measured after.

**H1 — `record_redirect` was never reached on an error-terminated chain.** The call sat on the
`location is None` success return only, so every other way out of the loop — a timeout from
`hop_deadline`, `max_redirects` exceeded, a transport failure — left the event unwritten and
`is_redirect` False on chains this library had provably followed. This is the *same dead flag*
round 2 was opened to fix, surviving on the exits nothing measured, and it violated the stated goal
at `constants.py:48` ("owning it ourselves must preserve the semantics, not invent new ones").
Measured before and after, against raw `aiohttp`:

| shape | hops followed | before | after | raw aiohttp |
|---|---|---|---|---|
| 302 → 302 → stalls past the deadline | 2 | `is_redirect=False`, event MISSING | `True`, 0.0012 | `True`, 0.0017 |
| `max_redirects=2`, 4-hop chain | 3 | `is_redirect=False`, event MISSING | `True`, 0.0026 | `True`, 0.0002 |

Fixed by wrapping the loop body in `try: … finally: record_redirect(...)`, which is the placement
that cannot be forgotten on a new exit — a `raise` added later is covered by construction, where
three separate `record_redirect` calls before three `raise`s would not be. **The refused-hop case
stays unrecorded** and is unchanged: `redirect_target` raises *before* `redirected_at` is assigned,
so the `finally` reads the `None` it started with, and `record_redirect` returns without writing.
That shape is pinned by the existing `test_a_refused_hop_is_not_recorded_as_a_followed_one` and by
mutation M2 below.

**H2 — the workaround `validated_trace_config` recommends produced the hollow output it exists to
prevent.** The refusal tells a caller with their own session to "pass `trace_configs` to your own
`ClientSession`", and on that route the library derived no collectors from the session at all, so
`record_redirect` wrote nowhere. Measured on a 2-hop chain the library followed: the caller's own
collector reported `is_redirect=False` and `on_request_redirect` MISSING; after, `True` / 0.0020.
Fixed with a new `trace_collectors_for(session, trace_config)` in `http_client.py`, which reads
`session.trace_configs` (public, and the same objects the caller constructed the session with) and
keeps only members carrying a writable `results_collector` — a caller's session may legitimately
carry a plain `aiohttp.TraceConfig` for their own callbacks, and failing their call on it would be
a new defect. **The refusal message remains true**: `trace_configs` is still session-constructor-
only and this library still cannot attach one to a session it did not create; what changed is that
it now *reads* what the caller attached instead of ignoring it.

*The envelope's contract is deliberately unchanged* — a supplied session still reports
`request_tracer == []`. The defect was the collector being unreachable, not the envelope being
empty, and widening the envelope has a cost of its own: those collectors outlive the call, so a
session reused across five calls would leave all five envelopes aliasing one mutating dict and the
first envelope silently reporting the fifth call's timings. `HttpRequest` therefore carries two
lists — `trace_collectors` (what the loop writes) and `reported_collectors` (what the envelope
shows) — which differ on exactly that one path. Recorded in `Decisions` above; mutation M6 pins the
choice, so widening it later is a deliberate act rather than a drift.

**M1 — `trace_config` was the last `protocol_info` key shipping unvalidated**, two lines below four
validated siblings, and it is the one round 2 touched. Every malformed spelling escaped `request()`
**un-enveloped**, measured:

| value | before | after |
|---|---|---|
| `[aiohttp.TraceConfig()]` | `AttributeError: 'TraceConfig' object has no attribute 'results_collector'` | `ConfigurationError` naming `request_tracer()` |
| `'tracer'` | `AttributeError: 'str' object has no attribute 'results_collector'` | `ConfigurationError`, bare-str refused by name |
| `aiohttp.TraceConfig()` (unlisted) | `TypeError: 'TraceConfig' object is not iterable` | `ConfigurationError`, "passed as a one-element list" |
| `[object()]` | `AttributeError` | `ConfigurationError`, member type named |

The first row is the likeliest mistake of the four and the reason the member check is not just an
`isinstance`: a plain `aiohttp.TraceConfig` looks exactly like what the key wants, and the
`results_collector` this library reads is attached by `request_tracer()`, not by `aiohttp`. All
refusals are at construction, asserted on the recorded request count staying at **zero**. An empty
list stays legal (it means "no tracing"), which is its own test — a guard one condition wider would
have refused it.

**M2 — recorded here as "investigated and refuted by measurement"; that conclusion was WRONG and is
corrected in round 4 below.** The claim as written in round 3 was: the reviewer suspected
`on_request_redirect` here is based on `chain_started` where `aiohttp` bases it on the last hop's
`on_request_start`; the reviewer's shape (one redirect, delay on the **first** hop) cannot
distinguish the two hypotheses because hop-1 start and chain start coincide there; so round 3 ran
what it called the distinguishing shape — **two redirects with the 0.30s delay on the second hop** —
against both libraries, three runs:

| run | async-gateway `on_request_redirect` | raw aiohttp `on_request_redirect` |
|---|---|---|
| 1 | 0.3025 | 0.3030 |
| 2 | 0.3041 | 0.3029 |
| 3 | 0.3034 | 0.3118 |

and concluded from the parity that `aiohttp` measures from the start of the exchange too, left
`redirected_at` based on `chain_started`, and pinned the shape as
`test_the_redirect_event_is_timed_from_the_last_hop_not_the_first`.

**That shape is degenerate in the other direction, and the numbers above are not evidence of
anything.** With the delay on the *last redirecting* hop, the delay falls inside that hop, so it is
counted by the chain-based measure and the hop-based measure alike — both hypotheses predict ~0.30
and the observed agreement discriminates nothing. Round 4 ran the shape that actually separates
them (delay on the **first** of two hops) and found a stable ~200-300× divergence. The finding was
real; see the round-4 section. The claim is left standing above rather than deleted because it was
acted on: the code shipped a round with a defect this entry said was not there, and the pin it
added asserted the opposite of the behaviour in its own name.

**M3 — recorded as an accepted cost, not changed.** See `Decisions` above: the per-invocation
deadline multiplies the caller's `timeout` by `allowed_retries + 1`; it is pre-existing base
behaviour, latent at the default `allowed_retries=0`; owner **S16**; revisit trigger **the first
story that raises `allowed_retries` above 0**.

**L2 — a docstring asserting a property the code does not have, corrected.**
`validated_max_redirects` claimed zero "is the natural spelling of a call that wants the redirect
response itself". Measured, it is not:

| ask | ok | code | status | text |
|---|---|---|---|---|
| `max_redirects=0` | `False` | `TRANSPORT` | 302 | `''` |
| `allow_redirects=False` | `True` | — | 302 | `'moved-body'` |

Zero is a bound the chain *overran*, so the loop raises where it would have followed and the 302's
body is never read. The **docstring** was corrected to say what the code does and to point at
`allow_redirects=False` for the "give me the redirect response" intent; the behaviour was **not**
changed to match the prose. Both asks are now driven side by side against one server in
`test_zero_redirects_and_no_redirects_are_different_asks`, so the difference is a measurement
rather than a claim — this run has shipped false prose twice already (AGW-35, and a test named
`..._pinhole_not_a_mute_switch` that was itself the false claim).

**L1 — documented, as directed.** `hop_deadline`'s `remaining <= 0` guard means `timeout=0` and
`timeout=-1` now fail `TIMEOUT`/504 **before dialling**, where base HEAD and raw `aiohttp` both
returned 200. This is an intentional behaviour change and is recorded here rather than fixed: it
follows from the chain-wide budget being real, and adding a `validated_timeout` would be new
validation surface on a key that has no validator today and that this story did not scope.

**Mutation proofs (round 3).** 8 mutations, applied one at a time, each **md5-confirmed as landed**
(a mutation that does not land proves nothing) and each restored from a `cp` taken before the run
with a **matching md5**. The harness also fails a run whose selector collects no tests — the first
pass reported six false survivors from a mis-quoted `-k`, which is exactly the shape of a mutation
proof that proves nothing.

| # | mutation | killed by |
|---|---|---|
| M1 | `record_redirect` moved back out of the `finally`, onto the success return only | the timeout and `max_redirects` trace tests |
| M2 | `redirected_at` hoisted *above* the scheme check | `test_a_refused_hop_is_not_recorded_as_a_followed_one` |
| M3 | no collectors derived from a supplied session (the H2 defect restored) | `test_the_workaround_the_refusal_names_actually_receives_the_event` |
| M4 | the usable-collector filter dropped, so a foreign `TraceConfig` is fatal | `test_a_foreign_tracer_on_a_supplied_session_is_skipped_not_fatal` |
| M5 | `validated_trace_config`'s new checks bypassed | the five malformed-`trace_config` rows |
| M6 | the envelope widened to the supplied session's collectors | the `request_tracer == []` assertion on the H2 test |
| M7 | `max_redirects=0` returns the 302 instead of raising (the false docstring made true) | `test_zero_redirects_and_no_redirects_are_different_asks` |
| M8 | the redirect event re-based on the last hop rather than the chain | `test_the_redirect_event_is_timed_from_the_last_hop_not_the_first` |

**All 8 killed**, each by the test written for it. **M8 is void in hindsight:** it "killed" the
correct behaviour, because the test it was killed by pinned the wrong one. Superseded by round 4.

**Gate (round 3).** `pytest` **651 passed, 2 xfailed** (the two SOAP xfails, unchanged; no new
xfails) · coverage **95.661451%**, identical across all five committed seeds · `mypy async_gateway`
clean, 24 files · `flake8 async_gateway tests` = **23**, the unchanged pre-existing set.
`fail_under` raised 95.55 → **95.66** (truncated) as the last edit. Coverage rose; nothing was
lowered.

---

## Work log — review round 4 (one finding: M2 was real)

Round 4 exists for a single finding, and it is a correction of round 3's own conclusion. Round 3
recorded M2 as "investigated and refuted by measurement"; the orchestrator re-measured during
VALIDATE and the refutation does not hold. The M2 entry above is corrected in place rather than
deleted — a wrong conclusion that was *acted on* belongs in the history.

**M2 (real) — `redirected_at` was based on the start of the chain where `aiohttp` bases it on the
last redirecting hop's own `on_request_start`.** `aiohttp`'s callback computed `loop.time() -
context.on_request_start`, and its trace context is **per request** — each hop of its chain is one
request, so the base instant was that hop's own start. This library owns the loop, issued each hop
as its own request, and then measured the event from a `chain_started` captured once at the top of
`make_http_request`. The two agree only when the last redirecting hop *is* the first hop, or when
everything slow happens inside the last redirecting hop.

*The shape is the whole finding.* Three candidate shapes, and what each one can prove:

| shape | chain-based predicts | hop-based predicts | discriminates? |
|---|---|---|---|
| 1 redirect, delay on it (the reviewer's) | ~0.30 | ~0.30 | **no** — the only hop is the first hop, so the two instants coincide |
| 2 redirects, delay on the **second** (round 3's) | ~0.30 | ~0.30 | **no** — the delay is inside the last redirecting hop, so both measures include it |
| 2 redirects, delay on the **first** | ~0.30 | ~0.00 | **yes** — the chain starts before the slow hop; the last hop starts after it and answers at once |

Both prior rounds picked a non-discriminating shape and read the resulting agreement as evidence.
Measured on the discriminating shape, `/a` --302(0.30s)--> `/b` --302--> `/c`, against raw `aiohttp`
driven over the identical fixture with the identical tracer, in one script so the harness is not a
variable — before the fix and after, three runs each:

| shape | run | before (ours) | after (ours) | raw aiohttp |
|---|---|---|---|---|
| delay on FIRST hop | 1 | 0.3064 | 0.0021 | 0.0002 |
| delay on FIRST hop | 2 | 0.3036 | 0.0003 | 0.0008 |
| delay on FIRST hop | 3 | 0.3057 | 0.0011 | 0.0002 |
| delay on SECOND hop | 1 | 0.3038 | 0.3015 | 0.3016 |
| delay on SECOND hop | 2 | 0.3054 | 0.3026 | 0.3021 |
| delay on SECOND hop | 3 | 0.3045 | 0.3024 | 0.3011 |

Before: a stable ~300× divergence on the first-hop shape, exact agreement on the second-hop shape —
which is precisely the pattern the table above predicts, and why round 3's agreement was worthless.
After: both shapes agree with `aiohttp`. This was a Medium on a top-level `GatewayResponse` key,
against the stated goal at `constants.py:48` — "owning it ourselves must preserve the semantics, not
invent new ones".

**The fix.** `chain_started` is gone. A new `measure_redirect(trace_collectors)` runs **at the hop**,
reading each collector's own `on_request_start` (the key `request_tracer.py` writes at every hop)
and returning `(collector, elapsed)` pairs; `record_redirect(event)` writes them after the loop.
Two things this shape buys, both of which were the honest-handling question:

* **Measured at the hop, written after the loop.** Reading the base instant in the `finally` would
  read the *next* request's start — `on_request_start` has been overwritten by then. The write still
  has to happen after the loop (a flag written at the hop is erased by the request the hop leads to,
  which is round 2's finding), so measure and write are now separate steps.
* **Per collector, and no fabricated zero.** Each tracer keeps its own clock reading, so two tracers
  that disagree are both reported honestly instead of one being silently picked. A collector with no
  usable `on_request_start` (absent, None, or non-numeric) gets `is_redirect = True` — the hop *was*
  followed — and **no** `on_request_redirect` key at all. A zero there would read as "the redirect
  was instantaneous", and key-absence is already this tracer's vocabulary for "did not happen".
  Tracing off (no collectors) is unchanged and costs nothing.

`request_tracer.py` was **not** touched (out of boundary). `is_redirect` semantics are unchanged,
including H1's record-on-every-exit and the refused-hop exclusion: `redirect_target` still raises
before the measurement is taken, so the `finally` reads None and writes nothing.

**The pin's name was false, and that is part of the finding.** Round 3 added
`test_the_redirect_event_is_timed_from_the_last_hop_not_the_first`, whose name asserts the exact
opposite of the code it pinned (timed from the chain, i.e. from the first), on a shape where its
assertion `>= LATE_HOP_DELAY` holds under both hypotheses. It discriminated nothing while reading as
though it discriminated everything. Replaced by:

* `test_the_redirect_event_is_timed_from_the_hop_not_the_chain` — the delay-on-**first**-hop shape,
  asserting the event comes in **under** `PROMPT_HOP_CEILING` (0.05s), which chain-based basing
  (0.3036-0.3064s measured) cannot do. Its docstring records both degenerate shapes and why each
  failed to separate the hypotheses.
* `test_a_delay_inside_the_last_hop_is_still_counted` — round 3's shape kept as the **counterweight**
  rather than the pin: a "fix" that just reported zero would pass the discriminating test and fail
  this one. Mutation 3 below proves that is not hypothetical.

Plus four unit tests on `measure_redirect` / `record_redirect` in `tests/helpers/test_request_helper.py`:
per-collector base instants, the three unmeasurable-base rows, the no-hop-followed path, and
tracing-off.

**Mutation proofs (round 4).** Each applied one at a time, **md5-confirmed as landed** before the
run and **md5-confirmed as restored** from a `cp` afterwards; the **whole suite** was run each time
(~3s) rather than a `-k` selector, since round 3's own harness produced six false survivors from a
mis-quoted one.

| # | mutation | md5 landed → restored | killed by |
|---|---|---|---|
| 1 | `redirected_at` re-based on `chain_started` (the defect restored verbatim) | `3634e222` → `1d679eb3` → `3634e222` | `test_the_redirect_event_is_timed_from_the_hop_not_the_chain`, **and nothing else** |
| 2 | a missing base instant measured as `0.0` instead of None | `3634e222` → `67d12770` → `3634e222` | the three `..._without_a_fabricated_timing` rows |
| 3 | `record_redirect` writes `0.0` unconditionally, ignoring the measurement | `3634e222` → `5d41e036` → `3634e222` | the three rows above **and** `test_a_delay_inside_the_last_hop_is_still_counted` |

Mutation 1 is the one that matters: under it the suite is `1 failed, 657 passed`, and the single
failure is the new pin. Round 3's suite would have passed it — which is the defect this round fixed.

**Learning (promote).** *A "distinguishing experiment" must be shown to distinguish.* State which
hypothesis each candidate shape would **falsify** before trusting its result. Both the reviewer and
round 3 chose shapes on which the two hypotheses predict the same number, and each read the
resulting agreement as confirmation. Agreement under a shape that cannot disagree is not evidence.
The cheap check is a two-column prediction table (as above) written *before* the measurement.

**Gate (round 4).** `pytest` **658 passed, 2 xfailed** (the two SOAP xfails, unchanged; no new
xfails) · coverage **95.701198%**, identical across all five seeds · `mypy async_gateway` clean, 24
files · `flake8 async_gateway tests` = **23**, the unchanged pre-existing set. `fail_under` raised
95.66 → **95.70** (truncated) as the last edit. Coverage rose; nothing was lowered.

## Work log — review round 5 (three code findings the owned loop introduced, one doc correction)

Round 5's three code findings are all the same shape: **owning the redirect loop moved a `Location`
header and a `timeout` value onto code paths that had never had to survive them**, because aiohttp
absorbed both before. Each was reproduced through the public `request()` against the loopback
fixture before it was fixed, and each is re-driven the same way after.

**C1 (Critical) — a hostile `Location` authority escaped `request()` as a bare `ValueError`.**
`redirect_target` guarded `urljoin` and `.scheme` inside a `try`, which is why a malformed IPv6
authority already answered `ConfigurationError`. But `urlsplit` parses the **authority lazily**: a
target whose port is out of range or is not a number splits without complaint and raises only when
something first reads `.port` — and that reader was `same_origin` (`request_helper.py:357-358`),
several statements past the guard. Measured before, driving a 302 whose `Location` names an
out-of-range port: `UNCAUGHT builtins.ValueError: Port out of range 0-65535`; a non-numeric port:
`UNCAUGHT builtins.ValueError: Port could not be cast to integer value as 'abc'`. aiohttp answers
this with a typed `InvalidUrlRedirectClientError`. A bare `ValueError` out of the entry point also
defeats the one-conversion-point contract — it is not an `AsyncGatewayError`, so `request()`
deliberately does not envelope it, and this library's own bug reporting rule turns a hostile header
into what reads as a library defect. **Fix:** read the authority (`parts.hostname`, `parts.port`)
inside `redirect_target`'s existing `try`, so the `ConfigurationError` already written two lines
below covers it. After: `ok=False`, `CONFIG`, status 400, **1** recorded request on both rows.

**C2 (High) — an empty `Location` was a self-redirect loop and an 11× request amplifier.**
`resp.headers.get('Location')` returns `''` for a present-but-blank header, `''` is not None, so the
loop entered — and `urljoin(target, '')` is `target`, so the library re-issued the *same* request
until `max_redirects`. Measured: one 302 with an empty `Location` produced **11** requests against
aiohttp's **1** (which returns the 302 itself); with `allowed_retries=3` configured, **44**. That is
a request amplifier a hostile endpoint arms with one empty header. **Fix:** a blank or
whitespace-only `Location` is treated as absent (`header.strip() or None`), which restores aiohttp's
answer. After: `ok=True`, status 302, the 302's own body, **1** recorded request — including with
retries on.

**C3 (Medium) — `timeout` was the one `protocol_info` key nothing validated, and S15 gave it a crash
site.** The chain deadline is `time.monotonic() + timeout.total` (`request_helper.py:785`), reached
from `http_client.py:661`. Measured: `timeout='ten'` → `UNCAUGHT builtins.TypeError: unsupported
operand type(s) for +: 'float' and 'str'`; `timeout=True` → **`ok=True`**, silently a one-second
deadline, a caller's config error answered as a successful call; `timeout=-5` → an immediate
`TIMEOUT`. S15 had already added `validated_max_response_bytes`, `validated_max_redirects`,
`validated_allow_redirects` and `validated_allowed_schemes` for exactly this class. **Fix:**
`validated_timeout` beside those four, same style, same `ConfigurationError`, rejecting `bool`
(`True` is an `int` of 1) and anything non-positive or non-numeric; `float` is accepted alongside
`int` because sub-second deadlines are ordinary and this suite's own timeout tests are written with
them. Called at the one construction site. After: all three rows raise `ConfigurationError` at
construction with nothing dispatched.

**C4 (Medium, doc only) — the accepted cost's revisit trigger was false.** It said the retry
multiplier stayed latent until "the first story that raises `allowed_retries` above 0". But
`allowed_retries` is a **caller-supplied** key —
`protocol_info['circuit_breaker_config']['retry_config']['allowed_retries']` — so a caller reaches
the multiplier today, on the call they are writing, with no story shipping first. Worse, the retry
loop amplifies **security refusals**, not only slow calls: `abortable_exceptions` defaults to none
and `retriable_exceptions` to everything, so a refusal this library raised on purpose is re-issued
like a transport fault. Measured through `request()`: a 302 to `ftp://` that the per-hop scheme check
refuses costs 1 request by default and **4** with `allowed_retries=3`; an over-cap body is the same,
1 against 4. The Decisions entry is corrected in place; the trigger is now **S16 itself**, and
`abortable_exceptions`'s default is handed to it explicitly alongside the deadline's placement. The
code fix stays out of scope on purpose — both live in `helpers/internal/circuit_breaker_helper.py`,
outside S15's declared file boundary.

**Tests added.**

* `test_a_location_that_is_not_parseable_is_a_configuration_error` — parametrised from one case to
  three: the pre-existing malformed-IPv6 row plus `port-out-of-range` and `port-not-a-number`, the
  two the lazy authority parse let through. No prior row exercised a malformed *authority*.
* `test_a_location_with_a_malformed_authority_is_enveloped` — the same two rows driven end to end
  through `request()` against the fixture, because the escape was end to end: a unit test on
  `redirect_target` cannot show that a `builtins.ValueError` no longer leaves the public entry point.
* `test_a_blank_location_is_absent_and_is_not_a_hop` — empty and whitespace-only, asserting
  `ok=True`, status 302, the redirect's own body, and a recorded request count of **1**. The count is
  the assertion that matters; the envelope alone was already `ok=False`/`TRANSPORT` before the fix
  and would not have distinguished eleven requests from one.
* Five `timeout` rows added to `test_a_redirect_key_that_cannot_form_a_call_is_refused` (`'ten'`,
  `True`, `None`, `0`, `-5`), which already asserts the refusal is at construction with nothing
  dispatched.
* `test_a_numeric_deadline_is_accepted_int_or_float` — the counterweight: a guard one condition too
  wide would refuse the sub-second float every timeout test in this suite is written with.

**Gate (round 5).** `pytest` **671 passed, 2 xfailed** (up from 658; the same two SOAP xfails, no
new ones) · coverage **95.74%** against a `fail_under` of **95.70** — up from 95.70, nothing lowered
· `mypy async_gateway` clean, **24 source files** · `flake8 async_gateway tests` = **23**, the
unchanged pre-existing set; the two edited modules contribute the same 7 findings as their
pre-edit copies. `fail_under` is left at 95.70 rather than re-ratcheted, since S15's own ratchet was
set in round 4 and this round is a defect fix on it.

**Learning (promote).** *A guard around a parse is not a guard around the parsed value when the
parser is lazy.* `urlsplit` returns successfully and defers the authority error to attribute access,
so wrapping the `urlsplit` call proved nothing about `.port`. The check that catches this class is
to ask, of every guarded parse, **which attributes the guard actually forced** — and to force the
ones the code downstream will read, inside the guard, on purpose.
