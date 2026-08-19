# AGW-16: Breaker facade + per-destination registry + clock/sleep seam

- **Status:** DONE
- **Story:** S16 — spec Step 14, size **L** — 6 files, 12 criteria, cannot be split (`docs/specs/v1_release_stories.md` §4, Phase 3; sizing exception §12)
- **Spec:** `docs/specs/v1_release_spec.md` — R24 all (Group K — Transport security and resilience) · R7-AC1, AC7 (Group B — Dependency upgrades and the resilience-library decision)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; `CircuitBreakerConfig` §seam, `get_breaker`; Orchestrator Ruling D
- **Decisions:** none — Ruling D pre-decides keep-with-facade; the ADR is AGW-23's
- **Files (declared scope):** +`helpers/internal/breaker_registry.py`, `tests/helpers/test_circuit_breaker.py`, `tests/fixtures/clock.py` · rewrite `helpers/internal/circuit_breaker_helper.py` · ~`helpers/internal/base.py`, `utils/constants.py`

## Why

R24 requires a circuit breaker that can actually open and is keyed per destination: today the breaker
is constructed per request, so it never accumulates state (H8). FI-4 binds H8 and M16 into one commit
because hoisting to a module global without a per-destination registry trades a dead breaker for one
flaky host opening the circuit for every destination — a worse outage than today's. R7-AC1 and AC7
land the seven fitness behaviours as tests here, with behaviour 7 asserted against the facade, because
Ruling D makes the facade — not the dependency — own the `clock`/`sleep` seam.

Closes findings: H8, M12, M13, M14, M15, M16, L9, L10. Discharges FI-4.

## Definition of Done

- **FI-4 — H8 and M16 land together;** hoisting to a module global without a per-destination registry trades a dead breaker for one flaky host opening the circuit for every destination, a worse outage than today's
- state survives across `request()` calls: `maximum_failures+1` failures → `CIRCUIT_OPEN`/`503` **without reaching the transport**
- keyed `(family, host, port)`; opening A leaves B reaching the transport; bounded LRU (default 256) by least-recent **use**
- **the seam lives in this library's facade (Ruling D):** `pyfailsafe` provably has none (`circuit_breaker.py:139,142`, `failsafe.py:103`), so the facade **owns the whole breaker state machine and the whole retry loop** — counting, transitions, the three callback invocations, the backoff wait — delegating **only** exception classification and backoff computation. `clock`/`sleep` are keyword-only with real defaults and are **not** part of the LRU key; `reset()` clears between tests. A full open→half-open→close cycle runs on a `FakeClock` + recording `sleep` with `time.monotonic` and `asyncio.sleep` **untouched**
- **this story does not wait on AGW-23** — the facade's interface is identical under keep/replace/vendor, which is what takes AGW-27 off an open dependency decision
- half-open tested by **advancing the injected clock**, never by sleeping
- non-zero `delay`/`max_delay`, `jitter=True`, exponential default; recorded durations show three retries spaced and two sequences **not synchronised**
- `backoff: Literal['constant','exponential']` replaces the magic `name=='backoff'`
- three parametrised config typos each raise `ConfigurationError` **naming the key**
- `maximum_failures=0` reachable; numerics accept `int`/`float`, **reject `bool`** (`0`, `True`, `1.5`, `"5"`)
- **M12:** two requests sharing one config leave the caller's dict unchanged
- **R7's seven fitness behaviours exist as tests here**, behaviour 7 asserted **against the facade** (a test against the dependency would be a test of a known-false proposition)

## Dependencies

- **blockedBy:** AGW-15
- **blocks:** AGW-19, AGW-23

## Decisions

_None recorded yet._

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`11eb432`](https://github.com/ajyadav013/asyncio-gateway/commit/11eb432), [`17cbab5`](https://github.com/ajyadav013/asyncio-gateway/commit/17cbab5), [`d1eef1c`](https://github.com/ajyadav013/asyncio-gateway/commit/d1eef1c), [`befac75`](https://github.com/ajyadav013/asyncio-gateway/commit/befac75); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


### Implementation — lane `lane/s16`, commits `11eb432`, `17cbab5`, `d1eef1c`

**Files.** New: `helpers/internal/breaker_registry.py`, `tests/fixtures/clock.py`,
`tests/helpers/test_circuit_breaker.py`. Rewritten: `helpers/internal/circuit_breaker_helper.py`.
Modified: `helpers/internal/base.py`, `utils/constants.py`, `tests/conftest.py` (registrations only,
per C-2).

**Outside the declared scope, and why.** `logic/ftp_client.py` (2 lines),
`logic/sftp_client.py` (2) and `helpers/internal/request_helper.py` (3) each call
`circuit_breaker.failsafe.run(...)`. The facade replaces `Failsafe` rather than wrapping it, so
`.failsafe` no longer exists and those five call sites become `circuit_breaker.run(...)`. It is a
forced mechanical rename with no behaviour change — the diff against `c5026c9` is five identical
substitutions and nothing else — but it is a boundary widening and is recorded rather than absorbed.

**Findings closed.**

- **H8** — the breaker was constructed in `BaseRequestClass.__init__` while the entry point builds a
  fresh protocol object per call, so every request met a zeroed count. It is now a registry *lookup*.
  Pinned by a test that drives three separate lookups (standing for three `request()` calls) and
  asserts the fourth never reaches the transport.
- **M16 / FI-4** — landed in the same commit, as FI-4 requires. Keyed `(family, host, port)` via a
  new `destination_of` in `base.py`; bounded LRU-by-use at 256.
- **M12** — the caller's `circuit_breaker_config` is read, never written. The old code wrote a live
  `RetryPolicy` into it, which the new validator would now reject as an unknown key — so the
  pollution was a correctness bug, not untidiness.
- **M13** — defaults are `delay=0.1`, `max_delay=10.0`, `jitter=True`, exponential.
- **M14** — `backoff: 'constant' | 'exponential'` replaces the magic `name == 'backoff'`.
- **M15** — typed validation; an unknown key raises `ConfigurationError` naming it.
- **L9** — absence, not falsiness, selects a default, so `maximum_failures=0` is reachable.
- **L10** — numerics accept `int`/`float` and reject `bool`.
- **R7-AC1/AC7** — the seven fitness behaviours exist as tests, behaviour 7 asserted against the
  facade.

**Corrections carried in from the first (lost) attempt's review.** Each was designed in rather than
rediscovered: `asyncio.TimeoutError` named explicitly in `RETRIABLE_FAILURES` (Critical);
half-open *claiming* the trial rather than reading state, released from `run`'s `finally`
(High); config validated before the registry lookup and discarded on a hit (High); the abort check
placed above the policy guard so a caller with no `retry_config` still has this library's own
refusals excluded from the count, and those refusals propagating as themselves rather than inside an
empty-`str()` `RetriesExhausted` (High).

**Verification.** `pytest -q` 751 passed / 2 xfailed, coverage 96.12% (floor 95.70, baseline 671/2).
`mypy async_gateway` clean — and clean again on the two new modules with `ignore_errors` disabled,
so it is not masked. `flake8 async_gateway tests` 17 findings, down from 23 at `c5026c9`, none in
files this story authored.

**Python 3.10 (`3.10.18`), the interpreter floor, run for real rather than reasoned about.**
`asyncio.TimeoutError is TimeoutError` is `False` and it is not an `OSError` subclass; with the
explicit entry removed from `RETRIABLE_FAILURES`, `isinstance(asyncio.TimeoutError(), ...)` against
the remaining tuple is `False` — the timeout would escape the loop un-retried and uncounted. Full
suite on 3.10: 748 passed, 3 failed.

**Pre-existing, confirmed, NOT fixed (outside this story's boundary).** Those 3 failures are
`test_ftp_client.py::test_a_transport_failure_maps_to_its_typed_error[timeout]` and two sftp
`[*-timeout]` cases, which map `asyncio.TimeoutError` to `CONNECT` rather than `TIMEOUT` on 3.10:
`ftp_client.py:85-90`'s ordered table comments that "`TimeoutError` is an `OSError` from Python
3.11", which is exactly the class split above, so on 3.10 the `OSError` row wins. Verified identical
at `c5026c9` in a throwaway worktree, so they pre-date this story. Same root cause as the fix above,
different files — they need a ticket of their own.

**Mutation proofs.** 14 guards each mutated inside the protected code, confirmed red, restored from a
`cp` backup (never `git checkout`), and the restored tree re-verified byte-identical by `diff`:
`asyncio.TimeoutError` removed from the retriable set (1 failed); half-open claim reduced to a pure
read (1); claim released from `record_success` instead of `finally` (1); abort check moved below the
policy guard (3); validation moved to the cache-miss path only (1); `move_to_end` deleted (1);
registry key collapsed to a constant (7); unknown-port sentinel `-1` → `0` (1); truthiness fallback
restored (3); `isinstance(x, int)` restored (32); unknown-key rejection neutered (5); magic
`name == 'backoff'` restored (5); thundering-herd defaults restored (2); caller's dict mutated (3).

One test did **not** kill its mutant on the first attempt and was rewritten until it did: the
"validated on every call" test warmed no cache, so it exercised only the miss path and passed against
an implementation that returns early on a hit — the very defect it names. It now warms the
destination with a valid config first (`d1eef1c`).

**One incident worth recording.** After the mutate/restore cycles the suite showed 2 failures against
a source tree `diff` proved identical to the committed one. Cause was stale `.pyc` files: rapid
write-restore within the same mtime granularity defeated bytecode invalidation. Purging `__pycache__`
restored green. Not a code defect — but it is exactly the shape of thing that reads as one.
