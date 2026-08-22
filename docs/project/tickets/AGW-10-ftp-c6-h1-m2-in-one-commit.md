# AGW-10: FTP: C6 + H1 + M2 in one commit

- **Status:** DONE
- **Story:** S10 — spec Step 8, size M (`docs/specs/v1_release_stories.md` §4, Phase 2)
- **Spec:** `docs/specs/v1_release_spec.md` — R15-AC1…7,9 *(AC5 README half → AGW-29; AC8 → AGW-19)* (Group F — FTP protocol correctness)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; protocol clients
- **Decisions:** none
- **Files (declared scope):** ~`logic/ftp_client.py`, `helpers/internal/filters_helper.py` *(`get_ssl_config` only)* · +`tests/logic/{__init__,test_ftp_client}.py`, `tests/fixtures/ftp.py`

> ## ⚠ AGW-10 owns the A6 monitoring obligation — an explicit checkpoint, not a footnote
>
> The premortem of record names A6 as the likeliest remaining failure: `asyncssh`/`aioftp` driven to
> 100% **branch** coverage through mocks alone, where the `aiohttp.web` answer gives nothing.
> **At Step 8, measure branch coverage of `logic/ftp_client.py` in isolation**
> (`pytest tests/logic/test_ftp_client.py --cov=async_gateway.logic.ftp_client --cov-branch`) and
> **record the number in this ticket's work log**. **Threshold ≈ 90%.** A plateau below ~90% without a
> live server means **A6 is failing** and the containerised-fixture cost arrives at **AGW-27 (Step 26)**
> instead of here. **On a sub-90% plateau: stop and escalate to the orchestrator before AGW-11 starts.**
> Do not absorb it with `# pragma: no cover` — the ceiling of 10 is the only thing between that and a
> hollow 100, and spending it here is exactly how the hollow 100 happens.

## Why

R15 requires FTP to execute at all, to default to FTPS, and never to silently downgrade. C6's
`UnboundLocalError` means the protocol has never run, so fixing it puts H1 (the SSL block outside the
`try`) and M2 (the unconditional `stat`) on a live socket for the first time. FI-1 makes the three
inseparable: a story that fixes only the `UnboundLocalError` ships a protocol that now executes with
its security and reporting defects live.

Closes findings: C6, H1, M2, M3, H10-ftp. Discharges FI-1.

## Definition of Done

- **FI-1 is absolute — a story that fixes only the `UnboundLocalError` is rejected.** Fixing C6 makes FTP execute for the first time, putting H1 and M2 on a live socket
- `verify_ssl` initialised before the branch; `flake8` reports no `F821`
- the SSL block moves **inside** the `try` (a forced `get_ssl_config` failure → populated envelope, `error.code=='TLS'`)
- `verify_ssl` defaults **`True`**
- **fail closed:** the value passed to `Client.context(ssl=)` is **never `None`** when true, and a non-TLS server fails with `TLS` rather than completing in plaintext
- explicit `False` honoured + warning
- `stat` no longer unconditional — parametrised `download`/`upload`/`remove` all `ok=True` (a successful delete is a success, not a `999` a retry re-attempts)
- connect **and** transfer bounded by `timeout`
- `return self.response`; FTP `xfail`s removed

## Dependencies

- **blockedBy:** AGW-9
- **blocks:** AGW-13, AGW-18

## Decisions

_None recorded yet._

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`8b7e148`](https://github.com/ajyadav013/asyncio-gateway/commit/8b7e148), [`d2a7516`](https://github.com/ajyadav013/asyncio-gateway/commit/d2a7516); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


_Empty — opened at stage 1g, before implementation._
