# AGW-17: A client TLS context that is actually a client TLS context

- **Status:** IN REVIEW
- **Story:** S17 — spec Step 15, size S (`docs/specs/v1_release_stories.md` §4, Phase 3)
- **Spec:** `docs/specs/v1_release_spec.md` — R23 all (Group K — Transport security and resilience)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; transport security
- **Decisions:** D1 (raise, not assert) · D2 (FTP error-contract change) · D3 (`ssl_context=` key retired)
- **Files (declared scope):** ~`helpers/internal/filters_helper.py` *(`get_ssl_config`/mTLS context only)*, `tests/helpers/test_filters_helper.py` · +`tests/fixtures/tls.py`
- **Files (actual, all within the boundary the orchestrator widened for AGW-36 and the dead arm):** the three above · ~`logic/ftp_client.py` *(the unreachable exception arm only)* · ~`tests/logic/test_ftp_client.py` · ~`tests/test_no_blocking_io.py` *(the AST-ban allowance)* · ~`pyproject.toml` *(one dev dependency)*

## Why

R23 requires a client TLS context that is actually a client context: `Purpose.CLIENT_AUTH` builds a
*server* context, so client certificates have never worked and hostname checking is not what the
caller thinks it is. M1 is the trap in the fix — `{'ssl': verify_ssl or True}` is always `True`, so the
flag is inoperative today, and the obvious cleanup refactor would turn an accident into a live MITM
switch. FI-2 makes the mTLS live-handshake test non-optional, because this fix makes client
certificates connect for the first time ever.

Closes findings: H28, M1. Discharges FI-2.

## Definition of Done

- `Purpose.SERVER_AUTH` then `load_cert_chain`; `grep -n "Purpose.CLIENT_AUTH"` → 0
- the implementation **asserts** `check_hostname is True` and `verify_mode == CERT_REQUIRED` before returning, so a refactor cannot silently weaken it; a test asserts both
- returned under `ssl=`, not the deprecated `ssl_context=`; a test asserts the keyword actually passed to the connector
- `{'ssl': verify_ssl or True}` — always `True`, so the flag is inoperative — replaced by an explicit decision (`False` disables + warns, `True` enables, absent → `True`). *This turns an accident into a decision; the obvious cleanup refactor would have turned it into a live MITM switch (M1).*
- **FI-2 — the mTLS test is not optional:** this fix makes client certificates connect **for the first time ever**, so a throwaway cert pair + a **loopback TLS server requiring client auth** must assert a real handshake through `get_ssl_config`, and a wrong/expired cert must fail with `TLS`
- a missing/unreadable/mismatched cert file → `ConfigurationError` **naming the file**; a passphrase-protected key → `ConfigurationError` naming the limitation
- written **once** against aiohttp 3.14.x — no migration, no re-run gate

## Dependencies

- **blockedBy:** AGW-15
- **blocks:** AGW-22

## Decisions

### D1 — The guard is a `raise`, not an `assert`. Deliberate deviation from R23-AC2.

R23-AC2 says the implementation "**asserts** `check_hostname is True` and
`verify_mode == CERT_REQUIRED` before returning". It raises `ConfigurationError`
instead. This is strictly stronger and the deviation is recorded here rather than
silently taken.

`python -O` strips `assert` statements from the bytecode outright. A security guard
written as one is therefore present in development and **absent in exactly the
optimised deployments that most need it** — the guard would look enforced in every
test run and enforce nothing in production. Everything AC2 asks for is delivered
(both properties are checked, before the context is returned, so a refactor cannot
silently weaken it); only the failure mechanism differs, and it differs in the safe
direction.

Proven, not asserted: `test_r23_ac2_the_guard_survives_python_dash_o` runs the same
probe program under two subprocess interpreters. Unoptimised it reports
`ASSERTS-LIVE GUARD-HELD`; under `-O` it reports `ASSERTS-STRIPPED GUARD-HELD`. The
first line is the control — it shows `-O` really was in effect and that an
assert-based guard would have vanished at exactly that point.

### D2 — FTP's error contract changes: an unloadable certificate moves 502 → 400.

**This is a live behaviour change for FTP callers, made deliberately and pinned by
test.** It follows from deleting the now-unreachable `except (IndexError,
TypeError)` arm at the old `ftp_client.py:359-370`.

| `protocol_info['certificate']` | Before | After |
|---|---|---|
| `('cert.pem',)` — one element | `CONFIG` / 400 | `CONFIG` / 400 *(unchanged)* |
| `5` — not a sequence | `CONFIG` / 400 | `CONFIG` / 400 *(unchanged)* |
| `('/no/such.pem', '/no/such.key')` — missing file | **`TLS` / 502** | **`CONFIG` / 400** |
| unreadable file / mismatched pair / not a PEM | **`TLS` / 502** | **`CONFIG` / 400** |
| passphrase-protected key | `TLS` / 502, message `[SSL] PEM lib` | **`CONFIG` / 400, naming the limitation** |
| system CA bundle unreadable | `TLS` / 502 | `TLS` / 502 *(unchanged)* |

400 is the better answer, on the same reasoning the two unchanged rows already used:
nothing was attempted, no packet left the process, and no retry can turn a path that
does not exist into one that does. 502 additionally placed the failure in the
retriable transport family, so a caller with a typo in their configuration had it
retried. It also aligns FTP with what the HTTP client already reported for the same
mistake — the split contract was itself a defect.

The old arm caught `(IndexError, TypeError)` around `get_ssl_config` because the
pre-R23 helper indexed `certificate[0]` and `[1]` unguarded. `normalised_certificate`
now rejects every one of those shapes itself with a `ConfigurationError`, so no input
can reach the arm; leaving it in place would be an uncoverable branch that measurably
lowers coverage (line 367 went uncovered) while suggesting a translation still
happens. The single `OSError` that still reaches the FTP handler is the *system* CA
bundle failing to read — raised by `ssl.create_default_context`, which sits
deliberately outside `build_client_ssl_context`'s own `try` because a broken trust
store is the environment's fault, not the caller's.

Both halves are pinned:
`test_r15_ac2_a_broken_system_ca_store_becomes_a_tls_envelope` (TLS/502) and
`test_r15_ac2_an_unloadable_certificate_becomes_a_config_envelope` (CONFIG/400).

### D3 — The `ssl_context=` key is retired outright, not emitted alongside `ssl=`.

R23-AC3 moves the certificate branch to aiohttp's supported `ssl=`. `ftp_client`'s
`TLS_CONFIG_KEYS` still reads both keys, which means a test tolerating either would
keep passing if the migration were reverted. `test_tls_context_for_keeps_the_callers_own_certificate_context`
therefore asserts `set(config) == {'ssl'}` — the exact key set, not membership.

## Work Log

### 2026-08-17 — implementation (lane `lane/s17`, worktree `wt/s17`)

**H28 — the client context is now a client context.**
`get_ssl_config`'s certificate branch builds through the new module-level
`build_client_ssl_context`, which calls
`ssl.create_default_context(ssl.Purpose.SERVER_AUTH)` then `load_cert_chain`.
`grep -rn "Purpose.CLIENT_AUTH" async_gateway/` returns two hits, both prose in
`logic/ftp_client.py` docstrings (lines 21 and 114) that predate this story and
explain the defect; deleting the explanation would be a loss, so R23-AC1's criterion
is enforced by AST instead —
`test_the_server_purpose_appears_in_no_executable_code` fails if any *executable*
statement in the package names it.

The failure is reproduced directly rather than only fixed:
`test_r23_ac1_a_client_socket_can_be_built_from_the_context` wraps a socket with both
contexts, and the defective one raises `ssl.SSLError: Cannot create a client socket
with a PROTOCOL_TLS_SERVER context` — the exact error that made the mTLS path fail
closed.

**FI-2 — the live handshake.** `tests/fixtures/tls.py` mints a real two-level PKI
(CA, server, client, rogue-CA client, expired client, mismatched pair, encrypted key,
non-PEM) into a temporary directory and runs a real loopback TLS listener with
`verify_mode=CERT_REQUIRED`. Client certificates authenticate for the first time
ever: `test_r23_ac5_a_good_certificate_reaches_the_caller_as_success` gets `ok=True`
/ 200 through the public `request()`, and rogue and expired certificates are refused
as `TLS` / 502. Three fixture details are load-bearing and each was found by a
handshake failing, so all three are documented in the fixture: `KeyUsage` on the CA
(else `CA cert does not include key usage extension` for *every* row),
`AuthorityKeyIdentifier` on the leaves (else `Missing Authority Key Identifier`), and
`maximum_version = TLSv1_2` on the server — under TLS 1.3 the client certificate is
sent after the server's half of the handshake completes, so a rejection arrives as
`ServerDisconnectedError` rather than an `SSLError` and would not classify as `TLS`
at all (measured 15/15 the wrong shape under 1.3, 15/15 the right one under 1.2).
The server uses blocking sockets on a thread for the same reason: under
`asyncio.start_server` the rejection arrives as a bare `ConnectionResetError`, which
classifies as `CONNECT`.

**M1 — the flag is a decision, three ways.** `{'ssl': verify_ssl or True}` was `True`
for every input including `False`. Replaced by an explicit three-way decision:
certificate → verifying context; `verify_ssl is False` **exactly** → `{'ssl': False}`
plus a `warning`; everything else, including `None` and absent → `{'ssl': True}`.
The identity test is the point — `test_r23_ac4_only_the_false_singleton_disables_verification`
parametrises `0`, `''`, `[]` and `None` and requires all four to verify, because the
obvious cleanup (`not verify_ssl`) reads identically and hands an unauthenticated
connection to every caller who passed one of them. There is no path to
`check_hostname=False` or `CERT_NONE` anywhere else in the module. A certificate
**overrides** `verify_ssl=False` (fail-secure: presenting an identity to an
unverified peer hands that identity to whoever answered) and this is documented on
the function and pinned by test.

**AGW-36 (Critical) — the containment allowance is deleted, not widened.**
`tests/test_no_blocking_io.py`'s `NOT_YET_REWRITTEN` is now `frozenset()`. It was
temporary scaffolding for this story: `ssl.create_default_context` reads the whole
194-certificate system CA bundle and `load_cert_chain` reads the caller's two files,
and on HTTP both ran on the event loop *inside* `failsafe.run` — once per retried
attempt. Both now run in `build_client_ssl_context`, a module-level plain `def`
reached only through `asyncio.to_thread`. The ban is satisfied rather than neutered.

*Containment proof (mutation, on the correct axis).* The protected property is "no
banned call inside a coroutine in `filters_helper`", so the mutation was made
**inside the protected function**: `def build_client_ssl_context` → `async def`,
after `cp`-ing the file. The scan went red with
`helpers/internal/filters_helper.py:207: ssl.create_default_context (in
build_client_ssl_context)` — the leaked call, in the function that leaked it.
Restored from the backup and re-run green. (A previous attempt at this story proved
the wrong axis: that a banned call in a *different* function was caught, which the
allowance never excused.)

Three tests hold the property from angles the AST scan structurally cannot see:
`test_agw36_the_certificate_is_loaded_off_the_event_loop` (thread identity — the scan
cannot see call sites), `test_agw36_the_loop_keeps_running_while_the_context_is_built`
(the property the identity is a proxy for), and
`test_agw36_the_blocking_builder_is_a_plain_def` (`asyncio.to_thread` on a coroutine
function silently never runs the work). The module docstring's "known limitations"
section is rewritten: the `load_cert_chain`-on-a-bare-variable gap is **still open**
even though its one instance is gone, and `ftp_client.tls_context_for` remains a live
AGW-37 instance of the module-level-`def`-without-executor shape.

**Lows.** A bad `certificate` now reports its **type**, not its arity — `b'ab'` was
admitted by a length check and rejected with "got 2 value(s)", and a 17-character
path string was rejected with "got 17 value(s)", both naming the wrong problem.
`os.PathLike` is accepted via `os.fspath` (`load_cert_chain` takes one natively;
refusing `pathlib.Path` invented a restriction OpenSSL does not have). A
passphrase-protected key is detected by a `password` callback that raises — OpenSSL
invokes it only for a key it cannot decode unaided — so the message names the
limitation instead of the `[SSL] PEM lib` a corrupt PEM also produces.
`except (ssl.SSLError, OSError)` is not written anywhere: `SSLError` **is** an
`OSError`, so one arm catches both. `cryptography` added to
`[project.optional-dependencies].dev` — `tests/fixtures/tls.py` and the pre-existing
`tests/fixtures/ftp.py` import it directly while it resolved only transitively
through `asyncssh`.

**Verification.** `pytest -q`: 720 passed, 2 xfailed, coverage 95.88% (baseline at
`c5026c9`: 671 passed / 2 xfailed / 95.74%; floor 95.70 unchanged and cleared).
Identical across all five committed shuffle seeds (1, 20250816, 424242, 99991,
2147483647). `mypy async_gateway`: clean, 24 files. `flake8 async_gateway tests`:
output **byte-identical** to a `git archive c5026c9` baseline (23 pre-existing
findings, no new ones). The CLIENT_AUTH grep returns the two `ftp_client.py` prose
hits and nothing else.

### Mutation evidence — every claim above was made falsifiable

A test that cannot fail proves nothing, so each load-bearing assertion was checked by
mutating the thing it protects, confirming red, and restoring from a `cp` backup. No
`git checkout`/`restore`/`stash` was used at any point.

| Mutation (inside the protected code) | Result |
|---|---|
| `Purpose.SERVER_AUTH` → `Purpose.CLIENT_AUTH` (reintroduce H28) | **15 failed**, incl. the client-socket reproduction and every live-handshake row |
| `if verify_ssl is False:` → `if not verify_ssl:` (the plausible M1 mis-fix) | **5 failed** — the absent-flag row and all four falsy parametrisations |
| the `raise` guard → `assert` (AC2 taken literally) | **2 failed**; run directly, the probe reports `ASSERTS-STRIPPED GUARD-STRIPPED` under `-O` versus `ASSERTS-LIVE`/`AssertionError` unoptimised — the guard is present in development and **gone in production**, which is D1's entire argument, executed |
| `def build_client_ssl_context` → `async def` (AGW-36 containment) | ban fires: `helpers/internal/filters_helper.py:207: ssl.create_default_context (in build_client_ssl_context)` |
| `await asyncio.to_thread(build_client_ssl_context, ...)` → direct call | **2 failed** — the thread-identity test and the loop-progress test |
| `{'ssl': context}` → `{'ssl_context': context}` (revert AC3) | **9 failed**, including the re-pointed FTP test — confirming it pins the new key rather than tolerating both |

The `async def` mutation is deliberately on the axis a previous attempt at this story
got wrong: it mutates **inside** the function the allowance protected, so the offence
reported is the leaked call in the function that leaked it — not a banned call in
some other function the allowance never excused.

The `to_thread` mutation initially failed only *one* of the two AGW-36 tests. The
loop-progress test had awaited its companion task before asserting, which guarantees
the companion ran regardless of what the code under test did. It was rewritten to
read the flag **before** the await; both tests now catch the mutation, and the
ordering is documented in the test as the reason it exists.
