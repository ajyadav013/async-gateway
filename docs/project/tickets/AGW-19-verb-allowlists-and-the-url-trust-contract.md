# AGW-19: Verb allowlists and the URL-trust contract

- **Status:** OPEN
- **Story:** S19 — spec Step 17, size **L** *(serialization point — 5 `getattr` sites in 5 files)* (`docs/specs/v1_release_stories.md` §4, Phase 3; sizing exception §12 — irreducible)
- **Spec:** `docs/specs/v1_release_spec.md` — R21-AC1,2,5 (Group J — The caller-controlled capability surface) · **R15-AC8** (Group F — FTP protocol correctness) · **R17-AC7** (Group G — SFTP protocol correctness)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; the capability surface
- **Decisions:** none — see §11-D3 (R15-AC8 / R17-AC7 forward-reference this step from Steps 8 and 10)
- **Files (declared scope):** ~`helpers/internal/base.py`, `logic/{http,ftp,sftp}_client.py`, `helpers/internal/request_helper.py`, `utils/http_file_config.py`, `tests/logic/test_ftp_client.py`, `tests/logic/test_sftp_client.py`, `tests/helpers/test_request_helper.py`

## Why

R21 requires bounded, documented verb allowlists and an explicit URL-trust contract. The defect *is*
that five `getattr` sites across five files reach for a caller-named attribute with no allowlist, so
fixing four of them still ships a fail-open surface — which is why §12 records this story as
irreducible. It also closes R15-AC8 and R17-AC7, whose text forward-references "the R21 allowlist"
from Steps 8 and 10 (§11-D3). Per-hop scheme enforcement already landed at AGW-15 under FI-16; this
story owns the entry-point contract.

Closes findings: M25, H13-ptr, M21-partial.

## Definition of Done

- each protocol declares an explicit allowlist; `grep -n "getattr("` shows **every remaining call preceded by an allowlist check in the same function**
- unknown verb → `ConfigurationError` naming the verb and listing allowed values — **fail closed**; parametrised over **all five sites** (`sftp_client.py:43`, `ftp_client.py:51`, `request_helper.py:31`, `:100`, `http_file_config.py:57`)
- `request_type='close'` — today `TypeError` swallowed as `999` — is now `ConfigurationError`
- a verb that exists but is not a coroutine function is covered; verb case/whitespace normalisation matches AGW-8's
- `allowed_schemes` defaults `{'http','https'}` and rejects the rest; `ftp://`/`file://`/`javascript:` passed to `'HTTP'` tested *(per-hop enforcement already landed in AGW-15 — FI-16)*
- **closes R15-AC8 and R17-AC7**, whose text forward-references "the R21 allowlist" from Steps 8 and 10 — see §11-D3

## Dependencies

- **blockedBy:** AGW-16, AGW-18
- **blocks:** AGW-20

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
