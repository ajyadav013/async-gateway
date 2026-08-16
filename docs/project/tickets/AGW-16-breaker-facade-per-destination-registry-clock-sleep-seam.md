# AGW-16: Breaker facade + per-destination registry + clock/sleep seam

- **Status:** OPEN
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

_Empty — opened at stage 1g, before implementation._
