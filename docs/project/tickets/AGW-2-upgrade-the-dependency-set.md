# AGW-2: Upgrade the dependency set — closes C7 in Phase 0

- **Status:** IN REVIEW
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

- **Orchestrator Ruling G — how the FI-11 clean-venv import check is run.** As literally written the
  check could not pass yet: the import chain `async_gateway.py → logic/__init__ → logic/http.py`
  reaches `import ujson` (also `response_helper.py:5`, `filters_helper.py:8`), and `ujson` has never
  been declared — that is finding **C1**, owned by AGW-5. The check exists to catch a module-level
  import broken by **aioboto3 15.x / asyncssh 2.24** (FI-11), so it was run with `ujson` pip-installed
  into the throwaway venv **only**, as a probe shim, never added to `requirements.txt`. Proven
  load-bearing and proven to mask only C1: uninstalling `ujson` yields exactly one failure
  (`filters_helper.py:8`) with nothing behind it. **Not unconditionally green — becomes genuinely
  clean at AGW-5.**
- **Orchestrator Ruling H — `requests` stays at `~=2.32.3`; NOT widened.** The developer escalated
  rather than choosing. pip-audit found 1 Medium on `requests` (PYSEC-2026-2275 / CVE-2026-25645,
  CVSS 4.4, insecure temp-file reuse in `extract_zipped_paths`). Ruled *leave*: AGW-5 deletes the line
  outright two stories from now, `requests` is imported by **zero** modules so the advisory is
  unreachable, and widening a soon-deleted phantom declaration is churn against a locked table. The
  DoD's absolute condition (zero Critical/High) is met either way. The reviewer independently
  confirmed leaving it breaks no other named criterion (R3-AC2 discharges at Step 4).
- `pyfailsafe==0.6.0` left as the sole exact pin — R7/Step 22 owns keep/replace/vendor.

## Work Log

**2026-08-16 — implemented, validated, reviewed, committed.**

*Change:* `requirements.txt` only — six runtime deps moved from exact/compatible pins to ranges with
major-version ceilings. `git status` confirms **no source file touched**.

### Evidence

- **Resolution on CPython 3.14.7** (the R1 matrix ceiling): single-pass, **zero backtracking**
  warnings, every native package resolving to a prebuilt `cp314` wheel — nothing built from source.
- **The 3.14 wall is cleared.** The orchestrator had established that the *old* pin `orjson~=3.10.6`
  cannot build on 3.14 (no wheel; PyO3 0.23.3 caps at 3.13). `orjson>=3.12.0,<4` resolves to a
  `cp314` wheel and imports. This is what makes R1's 3.10–3.14 matrix achievable at AGW-4.
- **`pyfailsafe==0.6.0` is green at the ceiling** — the flagged dormancy risk did not materialise;
  pure `py3-none-any` wheel, `import failsafe` succeeds on 3.14.7. All nine runtime packages import.
- **FI-11:** top-level entrypoint import plus the four packages-under-upgrade submodules
  (`http_file_config`/aioboto3, `logic.sftp`/asyncssh, `logic.ftp`/aioftp, `request_tracer`/aiohttp)
  all import. **Neither aioboto3 15.x nor asyncssh 2.24 breaks a module-level import**, so Step 1.5's
  stated contingency (pulling R25's S3 call-site repair forward) is **not triggered**; R25 stays whole
  at Step 18.
- **Orchestrator's own re-run** of AGW-1's fixture round-trip against the now-declared transport:
  `3 passed`, exit 0, on `aiohttp 3.14.3` / Python 3.14.7. Coverage total unchanged, ratchet untouched.
- **pip-audit, absolute counts on the resolved 33-distribution closure (FI-12 — never a delta against
  the audit's "38 in 3 packages"): Critical 0 · High 0 · Medium 1 · Low 0.** Condition met.

### Code review — APPROVED (1/5)

0 Critical · 0 High · 0 Medium · 1 Low. Reviewer verified the six ranges character-for-character
against the locked table with no floor moved, both deliberate exceptions still declared, line count
conserved 9 → 9 (nothing dropped under cover of the rewrite), and **both inline comments' factual
claims** — `date_helper.py:9` does bind `pytz.timezone()` at module scope, and `requests` does have
zero importers.

*Low carried forward:* the `pytz` comment cites `helpers/common/date_helper.py`; the repo-root path is
`async_gateway/helpers/common/date_helper.py`, so a grep from the root misses it. Cosmetic-adjacent,
non-blocking; fold into AGW-3 when this table is transcribed into `pyproject.toml`.

*Commit:* see `git log --grep "\[AGW-2\]"`.
