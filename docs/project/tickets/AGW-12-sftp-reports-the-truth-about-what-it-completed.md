# AGW-12: SFTP reports the truth about what it completed

- **Status:** OPEN
- **Story:** S12 — spec Step 10, size S (`docs/specs/v1_release_stories.md` §4, Phase 2)
- **Spec:** `docs/specs/v1_release_spec.md` — R17-AC1…6,8,9 *(AC7 → AGW-19)* (Group G — SFTP protocol correctness)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; protocol clients
- **Decisions:** none
- **Files (declared scope):** ~`logic/sftp_client.py`, `tests/logic/test_sftp_client.py`

## Why

R17 requires SFTP to report the truth about the operations it completed: an uninitialised
`remote_files` turns successful transfers into failures, and a destructive `remove` reported as a
failure is retried — deleting is not idempotent from the caller's point of view. M28's shared
`additional_arguments` dict means a directory `get` followed by a single-file `remove` on one
`protocol_info` silently loses `recurse=True`. L6's dead, mistyped `self.remote_files` is deleted in
the same commit because it is the thing that *looks like* the missing initialisation.

Closes findings: H2, M4, M28, L5, L6, H10-sftp.

## Definition of Done

- `remote_files` initialised before the branch; parametrised `get`/`put`/`remove` × {file, directory} → `ok=True` for all six
- **destructive success reported as success:** `remove` on a file → `ok=True`, `200`, and **not retried** (asserted by call count)
- `self.remote_files` (L6) — dead, mistyped, and the thing that *looks like* the missing initialisation — **deleted in this same commit** so it cannot mislead the next reader
- metadata from typed `SFTPAttrs`, with tests for a repr fragment lacking `':'` (today `IndexError`) and a missing `type` key (today `KeyError`)
- **M28:** `additional_arguments` copied — a directory `get` then a single-file `remove` **sharing one `protocol_info`** leaves the second without `recurse=True` and the caller's dict unchanged
- `connect_timeout`/`login_timeout` from `self.timeout`
- `return self.response`; `tat`→`latency`; `mode`/`files`/`file_stats` under `protocol_details`; docstring no longer says "ftp request class"

## Dependencies

- **blockedBy:** AGW-11
- **blocks:** AGW-18

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._
