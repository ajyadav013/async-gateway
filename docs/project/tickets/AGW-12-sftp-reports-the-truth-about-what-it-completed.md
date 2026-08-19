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

- `remote_files` initialised before the branch; parametrised `get`/`put`/`remove` × {file, directory} → `ok=True` for five, and an honest typed failure for directory + `remove` (**Ruling T**, review iteration 2: `asyncssh`'s `remove` cannot delete a directory, so the spec's "all six" was unachievable)
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

- **`mode` is validated in `handle_request`, not in `__init__`.** The R17 edge case ("`mode`
  absent → `ConfigurationError`, not `AttributeError` on `None.lower()`") does not say where. The
  constructor was tried first and is wrong: R11-AC3
  (`tests/test_entrypoint.py:209`) requires `SFTPRequest` to be **constructible with no
  `protocol_info` at all**, and raising there broke ten existing entry-point tests. The check now
  runs at the top of `handle_request`, still before the connect, and is reported as a
  `CONFIG`/400 envelope through the entry point's one conversion point.
- **`file_stats['type']` is a name, not asyncssh's integer.** `SFTPAttrs.type` is a
  `FILEXFER_TYPE_*` int; the envelope reports `'file'` / `'directory'` / `'symlink'` / `'special'`
  / `'unknown'`. A caller should not have to import asyncssh's constants to read an envelope. It is
  a vocabulary of this library's own: it is **not** the old string parse's (which republished
  asyncssh's own repr fragments, leading space included — `' regular'`), and FTP's `file_stats` is
  aioftp's stat mapping (`ftp_client.py:298,313`) and carries no such vocabulary to match.
- **`FILE_STAT_FIELDS` is an explicit list, not `SFTPAttrs`' every field.** The projection is the
  envelope's documented shape, so it changes when someone decides to change it — not because
  asyncssh added a field.
- **`recurse=True` is added only for the modes that accept one** (`RECURSING_MODES`). Reversed in
  review iteration 2: the first pass preserved the pre-existing "any mode on a directory" behaviour
  on the grounds that no R17 criterion covered it, which was wrong — against the real library it is
  a `TypeError` on every directory `remove`, in no transport family, escaping `request()` uncaught.
  See the iteration-2 log below.

## Work Log

### Implementation (S12 / lane β, wave W10)

`async_gateway/logic/sftp_client.py` — rewritten below the host-key layer S11 landed:

- **H2** — `_run_session` *returns* `(attrs, remote_files)` instead of leaving `remote_files` bound
  only inside the directory branch, so the shape that raised `UnboundLocalError` after a completed
  transfer is unavailable rather than merely fixed. `files` is `None` for a non-directory and `[]`
  for an empty directory.
- **The blanket handler** is gone. Failures are classified by `transport_error_for` into the typed
  hierarchy (`SftpStatusError` from the server's own `SSH_FX_*` code via `SFTP_STATUS_BY_FX_CODE`,
  then the transport families, then `TransportError` for any other `asyncssh.Error`); anything
  belonging to no family propagates, so a library bug is no longer reported as a failed call.
  `CircuitOpen` gets its own clause (503) ahead of the generic `FailsafeError` catch.
- The fabricated status, the `'tat'` key and both `time.time()` durations are deleted; the envelope
  is closed with `finalise_ok(..., status_code=200, started=self.start_time)`, which is the
  monotonic clock the entry point measures from.
- **M4** — `file_stats_for` reads the typed `SFTPAttrs` fields; the `str()`/`split(':')` parse that
  raised `IndexError` or `KeyError` *before the transfer was attempted* is gone. Directory
  detection is `attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY`, which is what asyncssh's own
  `_type()` helper uses.
- **M28** — `_run_operation` copies `additional_arguments` before adding `recurse`; nothing writes
  to the caller's dict.
- **L6** — `self.remote_files` deleted. **L5** — `__init__`'s "ftp request class" docstring
  corrected. **H10-sftp** — `connect_timeout` / `login_timeout` passed from `self.timeout`.
- **R17-AC7 not implemented** — the verb allowlist before `getattr` is AGW-19's, which fixes all
  five `getattr` sites at once. `_validate_mode` checks only that a name exists to look up.

`tests/logic/test_sftp_client.py` (+34 tests), `tests/fixtures/sftp.py` — `StubSFTPClient` now
records each call's **keyword options** (M28 asserts a keyword is absent, which a positional log
cannot see) and takes `attrs` / `entries` / `lstat_error` / `operation_error`. The two attribute
shapes that used to crash the parse are real `SFTPAttrs` objects (`UNPARSEABLE_ATTRS`,
`TYPELESS_ATTRS`), not mappings shaped like what the old client hoped to find.

`tests/test_envelope.py` — removed the two `strict=True` xfail markers this story earns: the SFTP
`CONTRACT_ROWS` row, and `test_e7_the_fabricated_status_appears_nowhere_in_the_package` (SFTP was
the last file in the package containing the fabricated status).

**Verification** (worktree `/tmp/agw-wt-s12`, venv `/tmp/agw-s12`):

- `pytest` → **391 passed, 2 xfailed** (both SOAP, AGW-22's), coverage **86.57%** (from 82.90%);
  `logic/sftp_client.py` **100.00%**. No test skipped or xfailed to get there.
- `mypy async_gateway` → **Success: no issues found in 24 source files**.
- `flake8 async_gateway tests` → **28** (from 32); zero in any file this story touched.

### Code review iteration 2 (H-1 / M-1 / M-2, Ruling T, Ruling H)

The reviewer found that iteration 1's finding #2 below was not a boundary observation but a live
defect the test suite was *certifying*. One root cause, fixed at the seam:

- **H-1 — the double was not signature-faithful.** `StubSFTPClient`'s operations took
  `*args, **kwargs` unconditionally, so they accepted keywords the real
  `asyncssh.SFTPClient` refuses. `tests/fixtures/sftp.py` now binds every recorded call against
  `inspect.signature(getattr(asyncssh.SFTPClient, name))` inside `_invoked`
  (`bind_to_real_signature`) — at the seam, so it covers the whole operation surface at once and
  keeps covering it when AGW-19's allowlist widens the mode set. This is the same principle
  `offered_identities` already applied to `client_keys`: a double that judges by its own rules can
  agree with a client that configures the real library wrongly.
- **M-1 — the `[directory-remove]` row was green because the stub swallowed the keyword.** With the
  binding in place it fails with `TypeError: got an unexpected keyword argument 'recurse'`
  (captured), which is the S11 failure mode of record — a test asserting the broken value works.
- **The fix — `RECURSING_MODES`.** `logic/sftp_client.py` narrows `recurse` to the six names
  asyncssh accepts it under (`copy`, `get`, `mcopy`, `mget`, `mput`, `put`), modelled on
  `ftp_client.py`'s `REMOVING_COMMANDS` and with the same care about spellings: naming only `get`
  and `put` would leave the defect live under `mget` the day AGW-19 admits it. `_run_operation`'s
  parameter is renamed `is_directory`, because "the target is a directory" is the fact and
  "`recurse`" was a conclusion drawn from it that only some modes can express.
- **M-2 — the M28 `remove` leg** drove `{'preserve': True}`, which `remove` also refuses. The second
  call now carries no `additional_arguments` and asserts `options['remove'] == {}` — realisable
  against the real library and a stricter statement of the leak (any keyword, not just `recurse`).
  The decisive `shared_arguments == {'preserve': True}` assertion is untouched: it reads the
  caller's own dict and is stub-independent.

**Ruling T (orchestrator, binding) — R17-AC1 is amended; the spec text is wrong here.**
`asyncssh.SFTPClient.remove` is *"Remove a remote file. This method removes a remote file or
symbolic link"* and takes `(path)` only; the directory operation is the separate `rmtree`. So
AC1's "asserting `ok=True` for all six" (spec lines 1211-1213) is **unachievable against the real
library**. Amended: five rows report `ok=True`; the sixth — directory + `remove` — reports an
honest typed failure (`ok=False`, `SFTP_STATUS`, 500, from the server's `SSH_FX_FAILURE` through
the existing `SftpStatusError` path). The double models that server rule. **`rmtree` was not
added**: silently escalating a `remove` into a recursive tree deletion is an implicit destructive
escalation the caller never named — the exact pattern eleven stories have been removing — and it
would be a new capability with no requirement behind it. The orchestrator is carrying the spec-text
correction to the acceptance gate alongside Ruling Q.

**Ruling H completed (authorised scope).** `tests/test_envelope.py` — `NOT_YET_REWRITTEN` is
deleted and the confinement assertion becomes `files_containing(pattern) == []`, a hard
package-wide ban. S12 is the designated completion point; all four patterns (`999`, `'tat'`,
`except Exception`, `time.time()`) verified at **0** package-wide before and after. The `<=` subset
form had left three of the four reintroducible into either client with the suite still green.

**Minor:** L-2 — the `FILE_TYPE_NAMES` comment no longer claims to keep the old parse's vocabulary
(the old parse yielded `' regular'`, leading space included). L-3 — the module docstring and
`handle_request`'s `Raises:` entry now name **both** `ConfigurationError` mechanisms and the line
between them: `_validate_mode` runs post-dispatch and arrives as an envelope, the host-key checks
run in `__init__` pre-dispatch and escape synchronously.

**Accepted cost (owner: the acceptance gate).** One class surfaces configuration errors two ways.
Both placements are correct under Ruling G, whose line is pre-dispatch vs post-dispatch and not
constructor vs call, and the split is inherited from R11-AC3 (which requires `SFTPRequest` to be
constructible with no `protocol_info`). No code change. **Revisit trigger:** reconcile by either
adding `mode` to `REQUIRED_INFO_KEYS` and amending R11-AC3, or moving the host-key checks into
`handle_request` — one line, for the whole class.

**Verification** (worktree `/tmp/agw-wt-s12`, venv `/tmp/agw-s12`):

- `pytest` → **391 passed, 2 xfailed**, coverage **86.60%** (floor 82.89); `logic/sftp_client.py`
  **100.00%**. No test weakened, skipped or xfailed.
- `mypy async_gateway` → **Success: no issues found in 24 source files**.
- `flake8 async_gateway tests` → **28**, unchanged; zero in any file this story touched.

### Findings outside this story's boundary (reported, not fixed)

1. `helpers/internal/base.py:137` — `_get_circuit_breaker_config` writes `retry_policy` into the
   caller's own `circuit_breaker_config` dict. Same family as M28, different file and owner.
2. `logic/sftp_client.py:_run_operation` passes `(remote_path, local_path)` positionally to every
   mode, but `asyncssh.SFTPClient.put(localpaths, remotepath)` takes them the other way round, so
   `mode='put'` appears to upload the remote path to the local one. Signature binding cannot catch
   it — both orders bind. Pre-existing, in no R17 criterion, and a behaviour change rather than a
   correctness fix to the findings under review; needs its own requirement.
3. `tests/test_envelope.py:291` — the `CONTRACT_ROWS` comment still says "Three of them are
   `xfail(strict=True)`"; after AGW-10 and this story only SOAP remains. Left alone per the
   two-edits-only boundary.
