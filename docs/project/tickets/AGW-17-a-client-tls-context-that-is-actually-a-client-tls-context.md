# AGW-17: A client TLS context that is actually a client TLS context

- **Status:** OPEN
- **Story:** S17 — spec Step 15, size S (`docs/specs/v1_release_stories.md` §4, Phase 3)
- **Spec:** `docs/specs/v1_release_spec.md` — R23 all (Group K — Transport security and resilience)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; transport security
- **Decisions:** none
- **Files (declared scope):** ~`helpers/internal/filters_helper.py` *(`get_ssl_config`/mTLS context only)*, `tests/helpers/test_filters_helper.py` · +`tests/fixtures/tls.py`

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

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
