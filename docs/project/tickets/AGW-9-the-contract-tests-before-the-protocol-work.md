# AGW-9: The contract tests, before the protocol work

- **Status:** DONE
- **Story:** S9 — spec Step 7, size S (`docs/specs/v1_release_stories.md` §4, Phase 1)
- **Spec:** `docs/specs/v1_release_spec.md` — R8-AC2 (Group C — The public response contract and the error model) · R28-AC8 partial (Group M — Quality gates turned on)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; the envelope contract
- **Decisions:** none
- **Files (declared scope):** ~`tests/test_entrypoint.py`, `tests/test_envelope.py`

## Why

R8-AC2 requires every protocol to return the same envelope key set, and the cheapest way to hold that
line is to write the assertion *before* the protocol work rather than after it. R28-AC8 requires each
test to name the requirement it proves. The tests land red for FTP, SFTP and SOAP by design — each
protocol story removes exactly its own `xfail` marker, so the contract is a ratchet rather than a
promise.

## Definition of Done

- parametrised no-network assertion `set(result.keys()) == EXPECTED_KEYS` across **5 protocols × {success, failure}**
- **expected to FAIL for FTP/SFTP/SOAP on landing — that is the point**; marked `xfail(strict=True)` per protocol so the ratchet stays honest and each protocol story removes exactly its own marker
- each test name references the requirement id it proves

## Dependencies

- **blockedBy:** AGW-8
- **blocks:** AGW-10, AGW-11

## Decisions

_None recorded yet._

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`c1c0a0b`](https://github.com/ajyadav013/asyncio-gateway/commit/c1c0a0b); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


_Empty — opened at stage 1g, before implementation._
