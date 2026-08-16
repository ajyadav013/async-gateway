# AGW-7: Envelope · timestamps · exceptions · logger · FI-7's HTTP half

- **Status:** OPEN
- **Story:** S7 — spec Step 5, size **XL** — 13 files, cannot be split (`docs/specs/v1_release_stories.md` §4, Phase 1; sizing exception §12)
- **Spec:** `docs/specs/v1_release_spec.md` — R8 (all but AC2, AC12), R9 all, R10 (all but AC3) (Group C — The public response contract and the error model) · R6-AC2 (Group B — Dependency upgrades and the resilience-library decision) · Part B invariants **E1–E11**
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; Architecture overview, the envelope contract
- **Decisions:** none
- **Files (declared scope):** +`utils/{envelope,status_map,redaction}.py` · rewrite `utils/exceptions.py` · ~`helpers/common/date_helper.py`, `utils/constants.py`, `helpers/internal/base.py`, `__init__.py`, `async_gateway.py`, `helpers/internal/request_helper.py`, `logic/http_client.py`, `pyproject.toml` · +`tests/{test_envelope,test_exceptions}.py`

## Why

R8, R9 and R10 are *the contract*: one response envelope returned by every protocol with a success
predicate that can fail, timezone-honest timestamps with monotonic latency, and a real exception
hierarchy whose errors are never empty. R6-AC2 removes `pytz` in the same commit as the last IANA
lookup, because the import and the declaration must move together or the clean-venv job breaks in one
direction. Part B's invariants E1–E11 land here as tests, which is what makes every later protocol
story checkable against a fixed shape. Three constraints forbid splitting this step (§12): FI-3 needs
the exception hierarchy and the envelope in one commit, FI-7's defect is *at the seam* so its fix must
be too, and the `pytz` atomicity rule binds the last two files together.

Closes findings: C2, H7, H27, H29, M20, MG3, L3, L4, L11, L12. Discharges FI-3 and FI-7 (HTTP half).

## Definition of Done

- `GatewayResponse` defined **once**; `new_envelope`/`finalise_ok`/`finalise_error` the only constructors; **E1–E11 exist as tests**
- `grep -rn "999" async_gateway/` → 0 (E7)
- `grep -rn "'tat'"` → 0
- `api_response` absent (test asserts `KeyError`)
- `grep -rn "except Exception" async_gateway/` → 0
- **FI-3:** `RetriesExhausted() from ClientConnectorError` yields a non-empty message + `cause` naming the connector; `unwrap_cause` depth-bounded, cyclic chain terminates *(a naive `str(exc)` yields `''`, which reads as success and is worse than 999)*
- **FI-7:** delete `request_helper.py:93`'s `kwargs.get('response', {})`; `make_http_request` returns typed `HttpResult`; `grep -rn "kwargs.get('response'"` → 0
- **E11:** a 404 with a JSON error body → `ok False`, `status_code 404`, non-empty `text` **and** populated `json`; `finalise_error` only adds `error` and flips `ok`
- UTC ISO-8601 `request_time`; `grep -rn "time.time()"` → 0; clock patched backwards still `latency >= 0`
- `grep -rn "Asia/Kolkata\|TIMEZONE\|pytz" async_gateway/` → 0 **and the `pytz` declaration drops in this same commit** (import and declaration must move together or the clean-venv job breaks in one direction)
- one redactor serving **both** envelope and logger — header, `?api_key=`, `{'password':…}` and `Set-Cookie` all absent from `repr(result)`; **no `redact=False` exists**
- exactly one handler on the `async_gateway` logger and it is a `NullHandler`; `CancelledError` propagates

## Dependencies

- **blockedBy:** AGW-6
- **blocks:** AGW-8

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
