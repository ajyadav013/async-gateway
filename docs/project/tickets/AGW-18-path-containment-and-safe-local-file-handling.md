# AGW-18: Path containment and safe local file handling

- **Status:** OPEN
- **Story:** S18 — spec Step 16, size M (`docs/specs/v1_release_stories.md` §4, Phase 3)
- **Spec:** `docs/specs/v1_release_spec.md` — R22 all (Group J — The caller-controlled capability surface)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; safe local file handling
- **Decisions:** none
- **Files (declared scope):** +`utils/paths.py`, `tests/utils/{__init__,test_paths}.py` · ~`utils/http_file_config.py`, `helpers/internal/request_helper.py`, `logic/ftp_client.py`, `logic/sftp_client.py`

## Why

R22 requires path containment and safe local file handling: on a recursive download the **remote
server** supplies the filenames, which is the Zip-Slip-shaped primitive — and C4's disabled host-key
verification (fixed at AGW-11) is what made an attacker-controlled server likely enough to matter.
One canonicalising `resolve_within` guards every write path, `safe_write` refuses to follow a
pre-created symlink and refuses to overwrite by default, and a `try/finally` guarantees a failed
transfer leaves no partial file behind.

Closes findings: M17, M18, M19.

## Definition of Done

- one `resolve_within(base, candidate)` canonicalises and asserts containment **before any write**; every write path routes through it
- parametrised traversal table (`../evil`, `../../etc/passwd`, `/absolute/path`, `a/../../b`, a null byte, an outward symlink) each → `PATH` and **writes nothing**, with the target directory asserted empty afterwards *(on a recursive download the **remote server** supplies the names — the Zip-Slip-shaped primitive, made likelier by C4)*
- `safe_write` uses `O_NOFOLLOW` + restrictive mode; a pre-created symlink is **refused, not followed**; default **refuse to overwrite** (`O_EXCL`) with opt-in `overwrite=True`
- where `O_NOFOLLOW` is unavailable, degrade to an explicit pre-write `lstat` check **with the same coverage**, documented
- `safe_unlink` idempotent (called twice on an absent file, no exception)
- a `try/finally` removes partial files — a forced mid-stream failure leaves none

## Dependencies

- **blockedBy:** AGW-15, AGW-10, AGW-12
- **blocks:** AGW-19

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
