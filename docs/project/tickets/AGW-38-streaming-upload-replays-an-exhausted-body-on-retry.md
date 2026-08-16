# AGW-38: Streaming upload path replays an exhausted body — attempt 2 uploads zero bytes

- **Status:** OPEN
- **Severity:** **High** — this is the sibling-path instance of already-registered finding **H9**
  (`docs/specs/v1_release_spec.md:2349`), and it takes H9's severity. (S14's artifacts briefly
  called it Critical; corrected to High so the register and the code comments agree.)
- **Story:** **S15** (spec Step 13, R14 — HTTP transport resilience), whose H9 acceptance criterion
  is verbatim *"body from a **factory invoked per attempt**"*.
- **Spec:** `docs/specs/v1_release_spec.md` — R14 / H9
- **Files (declared scope):** ~`async_gateway/helpers/internal/request_helper.py`
  (`make_http_filters_with_stream_file_upload`, lines 280-309) — already inside S15's declared
  boundary.

## Why

`make_http_filters_with_stream_file_upload` passes a **single** `file_upload(...)` async generator
into `circuit_breaker.failsafe.run`. An async generator is one-shot: the first attempt consumes it,
and every retry sends an empty body while the server answers 200 — a wrong-content upload that
reports success.

Measured during the S14 review, against a real loopback recording server, with the S14-fixed
sibling as the control:

```
STREAMING path (this ticket), 2 attempts, 960-byte file:
   attempt 1: raw body =  960
   attempt 2: raw body =    0        <-- zero bytes, server answers 200

NON-STREAMING control (fixed by S14), 2 attempts:
   attempt 1: raw body = 1147  part = 960
   attempt 2: raw body = 1147  part = 960
```

Re-measured at iteration 4 with a different file size: `[8192, 0]` versus the fixed path's
`[8370, 8370]`.

The **same** function also still shows the local-error-as-remote-failure symptom that S14 fixed on
its own path (that was S14's finding C2):

```
STREAMING, missing file via request():   ok=False  502 CONNECT
   "RetriesExhausted: [Errno 2] No such file or directory: '/.../never.bin'"
STREAMING, directory via request():      ok=False  502 IsADirectoryError, local path in message
```

Both paths are reached from the same dispatcher, `handle_http_request:495-499`, selected purely by
the presence of the config key `file_upload_chunk_size`. So which of the two behaviours a caller
gets — correct retries, or silent zero-byte uploads — depends on one optional config key.

## Why S14 did not fix it

Pre-existing and out of boundary. `git show 78d97a5` confirms the function is **byte-identical to
base**, and R20 does not reach it (it contains no blocking I/O — it was already `aiofiles`-based).
S14's four declared sites were `request_helper.py:69`, `:120,126`, `:185`, and
`http_file_config.py:78`; this function is none of them.

What S14 *was* required to fix was the **silence**: its own test docstrings described
"a single file handle — or any one-shot body — opened outside `failsafe.run` … uploads an empty part
while the server answers 200" as the defect the conversion had to avoid, which left a reader
believing the class was closed for uploads. It was closed for one of the two paths. S14 now names
this function and this ticket at
`tests/helpers/test_request_helper.py` (the `test_a_retried_upload_sends_the_whole_file_every_attempt`
docstring).

## Definition of Done (for S15)

- The streaming upload body is produced by a **factory invoked per attempt**, exactly as S14 did for
  the non-streaming path — not a single generator instance.
- A test drives **two** attempts and asserts attempt 2 sends the file's **real byte length**. A
  zero-byte upload is legitimate for an empty file, so the assertion must be against the real
  length, never merely "non-zero".
- The local-file failure modes (missing / directory / unreadable) raise to the caller rather than
  becoming a 502 `CONNECT` envelope with the local path in the message — matching the behaviour S14
  established on the sibling path.
- Coverage: `request_helper.py:294-303` and the dispatch branch at `:497` are currently **untested**
  (`--cov-report=term-missing`), which is the mechanical reason this survived. The fix must close
  that gap.
- Consider whether the two upload paths should converge; `make_http_filters_without_stream_uploads`
  is now a false name because both paths stream. Naming is optional here, correctness is not.

## Dependencies

- **blockedBy:** nothing (S15 is the next story in step order)
- **blocks:** S15's H9 criterion cannot be honestly claimed complete while this path is unfixed

## Work Log

- **2026-08-17 (S14 review, iteration 3):** found by the `sdlc-code-reviewer` under measurement
  while checking whether S14's artifacts over-claimed. Reading alone would have missed it — nothing
  in S14's diff points at the sibling function. Routed to S15 rather than fixed, since the code is
  pre-existing and out of S14's boundary.
- **2026-08-17 (S14 review, iteration 4):** severity reconciled from Critical to High to match
  registered finding H9; routing to S15 re-confirmed against `docs/specs/v1_release_stories.md:152`.
