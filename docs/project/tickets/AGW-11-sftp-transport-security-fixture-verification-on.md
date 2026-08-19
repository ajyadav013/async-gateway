# AGW-11: SFTP transport security, fixture verification-on

- **Status:** OPEN
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
- `client_keys=[]` by default (ambient `~/.ssh/id_*` never offered); supplied list forwarded unchanged
- no `~/.ssh/known_hosts` → fails closed naming the option
- **FI-9:** the fixture is written with verification **on from the start** — AGW-12's tests are written against it, so it must be right first

## Dependencies

- **blockedBy:** AGW-9
- **blocks:** AGW-12

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
