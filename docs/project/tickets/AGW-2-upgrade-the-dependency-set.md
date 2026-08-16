# AGW-2: Upgrade the dependency set — closes C7 in Phase 0

- **Status:** OPEN
- **Story:** S2 — spec Step 1.5, size S (`docs/specs/v1_release_stories.md` §4, Phase 0)
- **Spec:** `docs/specs/v1_release_spec.md` — R6-AC1,3,4,5,6 (Group B — Dependency upgrades and the resilience-library decision), R3-AC2 (Group A)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; Dependencies §Runtime (target state)
- **Decisions:** none
- **Files (declared scope):** ~`requirements.txt` **only** — no source file may change

## Why

R6 requires the upgrade to the pre-approved dependency versions with library pins loosened to ranges;
R3-AC2 requires the runtime dependency set to match what the code actually imports. The upgrade is
pulled into Phase 0 so that every later step — CI, transport resilience, TLS, S3, the tracer — is
written once against the target set rather than migrated later.

Closes findings: C7, H23. Discharges FI-10, FI-11, FI-12.

## Definition of Done

- Ranges with approved floors + major ceilings (`aiohttp>=3.14.3,<4`, `orjson>=3.12.0,<4`, `aioboto3>=15.5.0,<16`, `aiofiles>=25.1.0,<26`, `aioftp>=0.28.0,<1`, `asyncssh>=2.24.0,<3`); `grep -c "=="` over the table → 0
- **`pytz` and `requests` deliberately stay** (AGW-7 and AGW-5 own their removal — dropping either here reddens the clean-venv import)
- **no aiohttp API migration**
- fresh venv resolves with no backtracking; `python -c "from async_gateway.async_gateway import request"` succeeds (the FI-11 catch, cheapest moment)
- AGW-1's fixture round-trip re-runs against the now-declared aiohttp
- advisory scan on the **resolved** set = **zero Critical/High** (absolute, never a delta against "38 in 3 packages")

## Dependencies

- **blockedBy:** AGW-1
- **blocks:** AGW-3

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
