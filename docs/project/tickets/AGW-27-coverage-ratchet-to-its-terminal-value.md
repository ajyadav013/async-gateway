# AGW-27: Coverage ratchet to its terminal value

- **Status:** DONE
- **Story:** S27 — spec Step 26, size M (`docs/specs/v1_release_stories.md` §4, Phase 6)
- **Spec:** `docs/specs/v1_release_spec.md` — R28-AC4,5,6,7,8 (Group M — Quality gates turned on)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; Reaching branch coverage on the hard shapes
- **Decisions:** none
- **Files (declared scope):** ~`pyproject.toml`, `.github/workflows/ci.yml`, `tests/**` — **no source file may be edited.** If source must change to be testable, that is a defect loop back to the owning story, not a widening of this boundary

## Why

R28 requires 100% line and branch coverage, enforced, with network boundaries mocked. This step is the
terminal value of the ratchet every earlier story has been raising, not a single flip — if `fail_under`
is still far below 100 when it begins, the ratchet was not maintained and that is a process failure to
escalate. The pragma ceiling of 10 is what stands between a real 100 and a hollow one (see the A6
monitoring obligation on AGW-10).

Closes findings: H20.

## Definition of Done

- `pytest` exits 0 at **exactly 100/100 line+branch** on a clean checkout and non-zero at 99.9%
- pragma count **≤ 10** and every pragma carries a `--` justification; two CI checks enforce both
- the **outbound**-network-disabled run passes with `127.0.0.1` allowlisted (two fixtures bind loopback: the `aiohttp.web` server and AGW-17's TLS server)
- `pytest -p randomly` green on all five committed seeds
- every criterion in the spec that names a test has that test, named for its requirement id
- **this step is a finish, not a flip** — if `fail_under` is still far below 100 when it begins, the ratchet was not maintained and that is a **process failure to escalate, not a number to negotiate down**

## Dependencies

- **blockedBy:** AGW-26
- **blocks:** AGW-28, AGW-29, AGW-30

## Decisions

- **The last uncovered arc was a coverage.py defect, and it is fixed by pinning the
  measurement core rather than by a pragma.** coverage.py 7.15.4 defaults to the
  `sysmon` core on CPython 3.12+; on 3.14.7 that core reports a false missing arc out
  of an `async with` body nested three deep whose context managers await in
  `__aenter__`. `download_file_from_url` is exactly that shape (`ClientSession` →
  `response` → `safe_writer`), which is why `429->431` was the last gap. Reduced to a
  24-line reproduction containing no project code: `sysmon` reports the arc missing
  while the runner's own assertion proves the line ran; `ctrace` reports 100%.
  `core = "ctrace"` is set in `[tool.coverage.run]`. This is not a relaxation —
  `ctrace` is coverage.py's own C tracer and remains its default on every interpreter
  in the matrix below 3.14, so the pin makes 3.10–3.14 measure identically. A
  `# pragma: no cover` here would have bought the number by hiding a *working* test
  behind a tool defect, which is the exact trade R28 exists to forbid.
- **`fail_under = 100`, untruncated.** Every earlier floor was truncated because a
  rounded value could land above the run that measured it. 100 has no such headroom,
  and the ratchet has no further step to take: from here the number is defended, not
  raised.
- **Zero pragmas, against a ceiling of 10.** Both categories the spec pre-authorised
  went unused. The `@abstractmethod` body on `handle_request` is covered by a subclass
  that calls `super()` — worth a test rather than a pragma, because `@abstractmethod`
  only blocks a subclass that declares *nothing*, and the half-written subclass that
  declares the method and never finishes it would otherwise return `None` into the
  envelope.

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`be57e14`](https://github.com/ajyadav013/asyncio-gateway/commit/be57e14); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


- **Measured before touching anything.** 98.90% line+branch: 13 missed statements and
  14 partial branches across 8 modules, and zero pragmas in the tree.
- **Closed all eighteen gaps with tests only.** `logic/ftp_client` (the non-digit reply
  code and the loop exhausting to `None`, the causeless `FailsafeError`, the
  `CircuitOpen` arm, the absent `server_path`), `logic/sftp_client` (the absent
  `remote_path`), `logic/soap_client` (a 1.2 Fault with no `Code` and with no `Reason`,
  the caller-supplied session, the `CircuitOpen` arm, the `decode_error` raise, and the
  abortable-transport arm), `logic/http_client` (both halves of the verb guard, the
  `CircuitOpen` arm and its redaction), `helpers/internal/base` (the abstract body),
  `helpers/internal/request_helper` (the `at_eof` loop exit, distinguished from the
  inner `break` by asserting the reader's exact call sequence), `utils/contained_io`
  (an `OSError` the classifier does not recognise, driven with real `ENOENT`/`ENOTDIR`
  rather than a mock) and `utils/http_file_config` (the version-skew arm).
- **The no-source-edit boundary held.** `git diff --name-only -- async_gateway/`
  returns nothing. Nothing needed a source change to become testable, so there was no
  defect to loop back.
- **Added the two pragma CI checks (R28-AC7)** and proved each in both directions
  rather than only observing them pass on a tree that has no pragmas: an unjustified
  `# pragma: no cover` exits 1 and is printed, a `--`-justified one exits 0, and a
  twelfth pragma trips the ceiling. Without the negative case a grep that matches
  nothing is indistinguishable from a grep that is broken.
- **Verify (green).** `pytest` → 1380 passed, **100.00%** line+branch (1899 statements,
  562 branches, 0 missed, 0 partial), exit 0. Identical under all five committed seeds
  (1, 20250816, 424242, 99991, 2147483647). The negative half of the DoD holds too:
  below 100 the suite exits 1, and `precision = 2` means the comparison is against
  `100.00`, so 99.9% cannot round up into a pass. `flake8 .` → exit 0, silent.
  `mypy async_gateway` → clean, no `ignore_errors`. Pragma count 0.
