# AGW-11: SFTP transport security, fixture verification-on

- **Status:** DONE
- **Story:** S11 — spec Step 9, size S (`docs/specs/v1_release_stories.md` §4, Phase 2)
- **Spec:** `docs/specs/v1_release_spec.md` — R16-AC1…5 *(AC6 → AGW-29)* (Group G — SFTP protocol correctness)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; protocol clients
- **Decisions:** none
- **Files (declared scope):** ~`logic/sftp_client.py` · +`tests/logic/test_sftp_client.py`, `tests/fixtures/sftp.py`

## Why

R16 requires SFTP to verify the SSH host key and to make key-based authentication reachable. C4 is
`known_hosts=None` — verification disabled outright, which is the MITM primitive that also makes
AGW-18's Zip-Slip-shaped path work likelier. The bypass becomes an explicitly named option that warns
on every use rather than an unlabelled default. FI-9 requires the fixture itself to be written with
verification **on from the start**, because AGW-12's tests are written against it.

Closes findings: C4. Discharges FI-9.

## Definition of Done

- `grep -n "known_hosts=None" async_gateway/` → 0; default is asyncssh's system resolution achieved by **omitting** the parameter
- `known_hosts`/`host_key` pinning; mismatch → `HOST_KEY` (495)
- bypass is `insecure_skip_host_key_check=True` **by name**, warns every use; a truthy `verify_ssl=False` does **not** enable it
- `client_keys=None` and `agent_path=None` by default (ambient
  `~/.ssh/id_*` and agent identities never offered); explicit keys are
  forwarded unchanged and agent use requires `SFTPAuth.use_ssh_agent=True`
- no `~/.ssh/known_hosts` → fails closed naming the option
- **FI-9:** the fixture is written with verification **on from the start** — AGW-12's tests are written against it, so it must be right first

## Dependencies

- **blockedBy:** AGW-9
- **blocks:** AGW-12

## Decisions

_None recorded yet._

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`8ee1033`](https://github.com/ajyadav013/asyncio-gateway/commit/8ee1033), [`d2a7516`](https://github.com/ajyadav013/asyncio-gateway/commit/d2a7516); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


_Empty — opened at stage 1g, before implementation._
