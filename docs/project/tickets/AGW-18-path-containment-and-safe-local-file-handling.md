# AGW-18: Path containment and safe local file handling

- **Status:** DONE
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

- **The boundary widened to the protocol path, and that is the story's main finding.** R22's user
  story names the recursive directory download as the vector, and that vector does not pass through
  this library's own code: `aioftp` and `asyncssh` take the server's entry names, do the recursion
  and all the filename arithmetic internally, and write through their own filesystem layer. Guarding
  only the four HTTP write sites would have shipped a story that discharged its criteria on paper
  and left the requirement's own threat model undefended. Containment is therefore installed at the
  one seam each library offers — `aioftp`'s `path_io_factory`, `asyncssh`'s `_begin_copy`
  destination filesystem — in `utils/contained_io.py`, a file outside the declared scope.
- **The containment base is `client_path`/`local_path` itself, not its parent.** Confining to the
  parent let `../victimdir/OWNED` land beside the target tree and still count as contained.
- **AGW-33 folded in.** Its Definition of Done required a requirement first: R22 now carries the
  normative operand-order paragraph (Revision 5).
- **Four `A003` flake8 findings accepted**, matching the `utils/envelope.py:50` precedent from
  AGW-7. `list` and `open` are the method names `aioftp.pathio.AsyncPathIO` and `asyncssh`'s
  `LocalFS` dispatch on — verified against the installed libraries — so renaming them disables the
  containment wrapper. No `noqa` was added, because the repo has no `noqa` precedent.

## Breaking change (for the CHANGELOG — AGW-31 / S31)

**A download to a path that already exists now raises `ConfigurationError` instead of silently
replacing the file.** R22-AC3 chooses *refuse to overwrite* as the default (`O_EXCL`); callers who
want the old behaviour pass `overwrite=True`, which is plumbed to `download_file_from_url(...)`, to
`http_file_download_config`, and to the FTP/SFTP `protocol_info`. This bites the README's own fixed
`/tmp/test.pdf` example on its second run — the prose obligation is recorded on AGW-29, which owns
`README.md`.

Two smaller behaviour changes ride with it: downloaded files are now created mode **0600** rather
than at the process `umask`, and `delete_local_file_path` is now **idempotent** (it previously
raised `FileNotFoundError` on an already-absent file, M19).

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`1493fe3`](https://github.com/ajyadav013/asyncio-gateway/commit/1493fe3), [`8578972`](https://github.com/ajyadav013/asyncio-gateway/commit/8578972), [`925c784`](https://github.com/ajyadav013/asyncio-gateway/commit/925c784), [`c1c0a0b`](https://github.com/ajyadav013/asyncio-gateway/commit/c1c0a0b), [`f1ce83d`](https://github.com/ajyadav013/asyncio-gateway/commit/f1ce83d); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


- **Implementation.** `utils/paths.py` (new): `resolve_within` canonicalises the candidate's
  *parent* and returns the final component **verbatim**, so a symlink at the target survives to be
  refused by `O_NOFOLLOW` instead of being resolved away; `guarded_opener` puts the refusal in the
  `os.open` flags rather than in a preceding `lstat`, so there is no check-then-open window;
  `safe_writer` cleans up partials in a `try/finally`; `safe_unlink` is idempotent.
  `utils/contained_io.py` (new) installs the same guarantees on the FTP and SFTP paths.
- **Protocol escapes reproduced before they were fixed.** Both were driven against the real
  clients with only the wire faked, and both wrote **outside** the target directory at mode 0644:
  FTP via `download`'s `destination / name.relative_to(source)` over a hostile listing, SFTP via
  `_copy` filtering `scandir` names that are `.`/`..` *exactly* and then `posixpath.join`-ing a
  name that merely *contains* a separator. After the fix both raise `PathContainmentError`, the
  victim directory stays empty, and an ordinary transfer still lands (at 0600).
- **AGW-33 fixed and closed here.** FTP `upload` and SFTP `put`/`mput` now pass **LOCAL SOURCE →
  REMOTE DESTINATION**; `get`/`mget`/`download` keep remote-first. A per-verb `LOCAL_IS_SOURCE`
  table in each client, not a conditional at the call site — one shared positional order is how the
  defect survived. Tests assert the **content** that arrived over a mirrored tree, since neither a
  signature-faithful double (S12) nor a recording one (S11) can see two path-like positionals swap.
- **Defect found while finishing.** The two protocol wrappers called the guarded open directly and
  let the raw `OSError` escape, so a refused overwrite surfaced as `FileExistsError` on FTP/SFTP but
  as `ConfigurationError` on HTTP. `_refusal` was made a public, synchronous `classify_refusal` and
  is now shared by all three seams; `safe_writer` hops it to a thread (R20), the wrappers are
  already in one. Four red tests in `tests/utils/test_contained_io.py` went green.
- **Guards mutation-proven** (mutate inside, confirm red, restore): the AGW-33 operand tables (6
  red); canonicalise-before-check (42 red across the containment suite, incl. the
  symlinked-component rows); `iter_capped` buffering the whole body before raising (3 red — the cap
  genuinely bounds memory); `resolve_within_async` run on the loop (1 red, the AST scan does **not**
  see it); `classify_refusal` run on the loop (1 red — a test was added for it, since the mutant
  initially survived); removing `O_NOFOLLOW` *and* the degraded `lstat` (3 red); dropping
  `overwrite` at the public `download_file_from_url` (1 red).
- **Verification.** `pytest -q` → **807 passed, 2 xfailed, 96.21%** (baseline at this lane's parent
  `c5026c9`: the 4 rows above were the only failures). `mypy async_gateway` → clean, 26 files.
  `flake8 async_gateway tests` → 25 findings vs 23 at `c5026c9`; the delta is the four accepted
  `A003`s above, and the lane *fixed* two pre-existing findings in `utils/http_file_config.py`.
- **Deferred, by ownership:** `README.md` prose → AGW-29 (recorded on that ticket); CHANGELOG entry
  → AGW-31; the `mput` allowlist decision and `copy`/`mcopy` dispatchability → R21 at S19.
