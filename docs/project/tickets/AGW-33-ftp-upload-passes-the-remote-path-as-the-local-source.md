# AGW-33: transfer verbs pass the remote path as the LOCAL source (FTP `upload`, SFTP `put`/`mput`)

- **Status:** DONE — fixed in S18 (`lane/s18`), pending the main session's merge
- **Severity: High.** Not Critical — it needs a misconfiguration-shaped precondition — but see
  *The silent-wrong-file case* below: in a mirrored-tree deployment both clients transfer the wrong
  file to the wrong destination and report `ok=True`.
- **Story:** none of the original 32 — one defect family found twice: the FTP half during S10
  (AGW-10, out-of-R15), the SFTP half during S12's review (AGW-12, out-of-R17).
  **Both routed to S18 (AGW-18) as a recorded boundary extension** — see *Routing* below.
- **Spec:** `docs/specs/v1_release_spec.md` — **no acceptance criterion covers either half.** R15
  scopes FTP to TLS posture and the removing-commands set; R17 scopes SFTP to envelope truthfulness
  and caller-dict mutation. Neither reaches argument order. This ticket is the record that the gap
  is nonetheless owned, and **S18 needs a requirement written for it** before S19 widens the mode
  set (S19's verb allowlist will admit `mput`, carrying the identical defect).
- **Design:** `docs/specs/v1_release_spec.md` — Part B, the FTP and SFTP `protocol_info` contracts
- **Decisions:** Orchestrator Ruling R (routing by file boundary), recorded in
  `.claude/CONTINUITY.md`
- **Files (declared scope):** ~`async_gateway/logic/ftp_client.py`,
  `async_gateway/logic/sftp_client.py`, `tests/logic/test_sftp_client.py`,
  `tests/logic/test_ftp_client.py` — both already inside S18's declared boundary

## Why

`async_gateway/logic/ftp_client.py:389-397` (`_run_command`) dispatches **both** transfer commands
through one argument order:

```python
operation = getattr(client, command)
if self.client_path:
    await self.circuit_breaker.failsafe.run(
        operation, self.server_path, self.client_path, write_into=True)
```

`aioftp.Client` takes `(source, destination)` on both verbs, but the two verbs point in opposite
directions:

| verb | aioftp signature | source is | destination is |
|---|---|---|---|
| `download` | `download(source, destination, write_into=)` | **remote** | local |
| `upload` | `upload(source, destination, write_into=)` | **local** | remote |

So the call is correct for `download` and **inverted for `upload`**: it reads `server_path` off the
*local* filesystem and writes it to `client_path` on the *server*. An upload therefore either fails
(no such local path) or, where a local path of that name happens to exist, silently transfers the
**wrong file to the wrong remote location** — and the envelope reports success, because
`finalise_ok` is reached and the following `client.stat(self.server_path)` stats the remote
`server_path`, which for an upload is exactly the path that was never written.

The `protocol_details` block at `:310-313` compounds the confusion by reporting `server_path` and
`client_path` with their documented meanings, which the upload path did not honour.

### The SFTP half — the same bug in the other client

Found independently during S12's code review, verified against installed asyncssh 2.24.0.
`async_gateway/logic/sftp_client.py:620-622` passes `(self.remote_path, self.local_path)`
positionally to **every** mode. The real signatures do not agree on that order:

| verb | asyncssh signature | verdict |
|---|---|---|
| `get` / `mget` | `get(remotepaths, localpath)` | correct |
| `put` / `mput` | `put(localpaths, remotepath=None, *, ...)` | **inverted** |
| `copy` / `mcopy` | `copy(srcpaths, dstpath)` | ambiguous — needs a requirement to say which |

Pre-existing, not introduced by S12: `git show d2a7516:async_gateway/logic/sftp_client.py:249-250`
carries the same order. S12 correctly refused to fix it (no requirement behind it, and Surgical
Changes forbids the drive-by), and correctly reported it.

`put` is reachable **today**: R17-AC7's verb allowlist is deferred to S19, and `_validate_mode`
checks only that the mode name resolves to an attribute.

### The silent-wrong-file case (why this is High, both halves)

Both clients glob/read the *source* operand on the local filesystem. Usually the remote path does
not exist locally, so the call fails honestly with `ok=False`. But in a **mirrored-tree
deployment** — local and remote sharing a path layout, a common shape for both protocols — a local
file *does* exist at the remote path, and the client uploads **the wrong source to the wrong
destination while reporting `ok=True`**. A success envelope over a wrong-file write is the
dangerous case, and it is the one that will not show up in testing against a scratch directory.

### Why no test caught it, and why no test will

S12 made its SFTP double **signature-faithful** (`tests/fixtures/sftp.py`, `bind_to_real_signature`
binding every call against `inspect.signature(getattr(asyncssh.SFTPClient, name))`). That is a real
improvement and it catches refused *keywords* and wrong *arity*. It cannot catch this: both operands
are path-like positionals, so a transposition binds cleanly. **Arity-and-name checking was never
going to catch semantics.** Any fix therefore needs a test with a modelled filesystem — assert
which path was *read* and which was *written*, not which arguments were passed.

The `[file-put]` / `[directory-put]` rows in `tests/logic/test_sftp_client.py` assert `ok=True` and
read as certifying `put`. They are not lies — they assert only what the double did, accurately — but
a future reader should not take them as evidence `put` is wired correctly. S12 carries a docstring
note pointing here.

## Routing

By the story breakdown's own file-boundary rule (§7: *a path in no boundary is out of scope for
everyone*), the fix belongs to the next unlanded story whose declared boundary already contains
`logic/ftp_client.py`. That is **S18 / AGW-18** (W13 γ — "Path containment and safe local file
handling", spec Step 16), whose boundary is
`utils/paths.py`, `utils/http_file_config.py`, `helpers/internal/request_helper.py`,
`logic/{ftp,sftp}_client.py`, `tests/utils/test_paths.py`.

S18 is the right owner on the merits and not merely by adjacency: its acceptance criteria require
that **every write path routes through `resolve_within(base, candidate)` before any write**. An
upload whose source and destination are transposed is precisely a write path whose operands were
never established, so S18 cannot discharge its own criterion over this function without first
fixing which argument is which.

Rejected alternatives: reopening S10 (landed at `8b7e148`; the defect is outside R15's criteria and
a second FTP commit would break one-commit-per-story for no gain), and a standalone story (would
serialise against S18 on the same file for a change S18 must read anyway).

`tests/logic/test_ftp_client.py` is **added** to S18's boundary by this routing. Checked against the
W13 concurrency set: S16 (α) and S17 (β) touch neither file, so W13 stays pairwise disjoint.

## Definition of Done

**A requirement is written first.** Neither half is covered by an acceptance criterion today, and
S18 must not fix behaviour the spec is silent on. Add a criterion to R22 (or a new sub-requirement)
stating, per verb, which operand is local and which is remote — including a ruling on `copy`/`mcopy`,
which are genuinely ambiguous. Then:

- **FTP:** `upload` and `download` no longer share one argument order; each passes
  `(source, destination)` in the direction aioftp defines for that verb.
- **SFTP:** the same, per mode, against asyncssh's real signatures — `get`/`mget` keep
  `(remote, local)`; `put`/`mput` become `(local, remote)`; `copy`/`mcopy` follow the new ruling.
  A per-mode operand-order table beside `RECURSING_MODES`, not a conditional at the call site: the
  reason this defect survived is that one positional order was applied to every verb.
- The asymmetry is stated in a comment naming *why* the verbs differ — a future reader's most likely
  mistake is to "simplify" the table back into one shared order.
- **Tests must model a filesystem, not record arguments.** Assert which path was *read* and which
  was *written*. A signature-faithful double cannot catch a transposition of two path-like
  positionals (proven in S12), and a double that merely records an argument cannot catch it either
  (the standing learning from S11). Include the mirrored-tree case explicitly: a local file present
  at the remote path must not be silently uploaded.
- The post-command `stat` targets the remote path for every verb.
- An upload/put whose local source does not exist fails with `ok=False` and never reaches
  `finalise_ok`.
- `protocol_details` reports the paths with the meanings their docstrings give them.
- The S12 docstring note in `tests/logic/test_sftp_client.py` pointing at this ticket is removed
  once the rows genuinely certify `put`.

## Dependencies

- **blockedBy:** AGW-18's own blockers (AGW-15, AGW-10, AGW-12) — this rides with S18
- **blocks:** nothing

## Decisions

- **Ruling R (orchestrator, this invocation):** route by file boundary to S18 rather than reopening
  the landed S10. Rationale above. Recorded rather than silent.

## Work Log

Fixed inside S18 (`lane/s18`), as Ruling R routed it. Against the Definition of Done, item by item:

- **A requirement was written first.** R22 now carries a normative operand-order paragraph
  (*Revision 5, AGW-33*): for an **upload** the operands are **LOCAL SOURCE → REMOTE DESTINATION**;
  the download-direction verbs stay remote-first. `copy`/`mcopy` are ruled **out of reach** — both
  operands are remote, so neither is local and the question does not arise; whether they are
  dispatchable at all stays R21's allowlist decision at S19.
- **FTP.** `download` and `upload` no longer share one order. `LOCAL_IS_SOURCE` in
  `logic/ftp_client.py` decides per verb; `_operands` reads it.
- **SFTP.** The same table in `logic/sftp_client.py`: `get`/`mget` keep `(remote, local)`,
  `put`/`mput` become `(local, remote)`.
- **A table beside the command set, not a conditional at the call site**, with a comment naming
  *why* the verbs differ — the stated most-likely future mistake is "simplifying" it back into one
  shared order, and both tables say so.
- **Tests model a filesystem and assert content.** `test_agw33_an_upload_sends_the_local_file_the_caller_named`
  (FTP) and `test_agw33_a_put_sends_the_local_file_the_caller_named` (SFTP) build the **mirrored
  tree** — a local file present at the remote path — and assert the bytes that reached the server
  are the local file's, not the mirrored one's. Transposed, the wrong file arrives under `ok=True`,
  which is the defect. Argument-order rows exist too, but as a supplement: as this ticket predicted,
  neither a signature-faithful double (S12) nor a recording one (S11) can see two path-like
  positionals swap.
- **An upload whose local source is absent fails with `ok=False`** and never reaches `finalise_ok`
  — `test_agw33_an_upload_whose_local_source_is_absent_fails`.
- **The post-command readback stats the remote path** for every verb —
  `test_agw33_the_readback_stat_targets_the_remote_path` (`ftp_client.py:498`,
  `sftp_client.py:633`).
- **The S12 docstring note** pointing at this ticket is gone; the `put` rows now genuinely certify
  the verb.

**Mutation proof.** Setting both tables back to one shared order (`'upload': False`,
`'put'/'mput': False`) turns **6 rows red** across the two clients, including all three
content-asserting ones; restored, green.

Verification for the whole lane is in AGW-18's work log.
