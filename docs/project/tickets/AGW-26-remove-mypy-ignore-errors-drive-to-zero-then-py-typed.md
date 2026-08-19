# AGW-26: Remove `mypy ignore_errors`; drive to zero; then `py.typed`

- **Status:** DONE
- **Story:** S26 — spec Step 25, size M (`docs/specs/v1_release_stories.md` §4, Phase 6)
- **Spec:** `docs/specs/v1_release_spec.md` — R27-AC5,6 (Group M — Quality gates turned on) · R4-AC5 (`py.typed`) (Group A — Scaffolding and quality-gate infrastructure)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** none — the OQ2 count discrepancy (40 vs 44) is ratification-only and **is not allowed to matter**
- **Files (declared scope):** ~`pyproject.toml`, `.github/workflows/ci.yml` · +`async_gateway/py.typed` · ~`tests/test_packaging.py` · ~the files mypy reports — **type conformance only**

## Why

R27-AC5 requires the type checker to actually report: `ignore_errors` turns a real error list into
`Success: no issues found in 22 source files`, which is H18 — a gate configured to see nothing.
R4-AC5 adds `py.typed`, and FI-13 fixes its position: publishing the marker before mypy is clean
exports this library's type errors into every downstream consumer's build, so it is added **last**,
only once the checker is green. AGW-3 deliberately excluded it for this reason.

Closes findings: H18. Discharges FI-13.

## Definition of Done

- `grep -rn "ignore_errors" .` returns nothing outside the spec and the audit report; `mypy async_gateway` exits 0
- `ignore_missing_imports` only per-module, each with a comment naming the package — **never globally**, or the gate is back where it started
- **`py.typed` is added only now, last** (FI-13): publishing it before mypy is clean exports this library's type errors to every downstream consumer's build
- a packaging test asserts `py.typed` is in the wheel
- the count discrepancy (40 vs 44, OQ2) **is not allowed to matter** — the criterion is count-independent

## Dependencies

- **blockedBy:** AGW-25
- **blocks:** AGW-27

## Decisions

- **No `# type: ignore` was added to make an error go away.** Four remain in
  `async_gateway/`, all genuine third-party stub gaps, each with a per-line
  category and a written justification: `request_tracer.py` ×2 (aiohttp types
  `trace_config_ctx_factory` as `type[SimpleNamespace]` but *calls* it, and
  `results_collector` is this library's own attribute on `TraceConfig`),
  `contained_io.py` ×1 (aioftp's `_open` takes a ~40-member `Literal` union
  that a `str` cannot narrow to), and `request_helper.py` ×1 (see the
  defect-loop item below).
- **`ignore_missing_imports` per-module, four packages, each named.** A global
  setting silences the *next* untyped dependency as well as the four known
  ones — which is how a setting meant to acknowledge four gaps becomes a policy
  of not noticing any. Verified against the installed versions: `failsafe`,
  `aioboto3`, `botocore`, `aiofiles`.
- **`types-aiofiles` was not added.** It exists on PyPI and would remove one
  override, but adding a dev dependency needs explicit approval and is outside
  this story's declared scope. Installed experimentally to see what it would
  surface — one further real error in `request_helper.py:219` — then uninstalled
  and left unchanged. Recorded as a `# shortcut:` marker beside the override so
  the upgrade path is named rather than forgotten.
- **`JsonBody` widened here rather than deferred again.** AGW-13 recorded it as
  "defect for whichever story owns `utils/envelope.py`" and said explicitly that
  it "should be fixed before or with R27 rather than discovered by it". Removing
  `ignore_errors` is precisely what would have discovered it. `DecodedJsonBody`
  in `response_helper.py` collapses onto it, because two names for one type is
  how they come to disagree again.
- **`base.py`'s `timeout` and `certificate` are `Any`, not narrower.** Both hold
  raw, not-yet-validated caller values; `Any` states that. `int` contradicted
  both HTTP subclasses, and `Tuple[Text]` was wrong on the absent case, the
  arity and the validation this class does not perform.

## Work Log

- **Measured before changing anything.** With `ignore_errors` removed, `mypy
  async_gateway` reported **24 errors in 9 files** — not the 40/44 the audit
  predicted, because AGW-24's annotation pass had already closed most of them.
  The criterion is count-independent (OQ2), so the discrepancy is recorded and
  not argued.
- **Fixed the annotations rather than the symptoms.** The largest group was
  `Optional` values annotated as though they could not be None
  (`ftp_client.command_/server_path/client_path`,
  `sftp_client.mode_/remote_path/local_path`) — `protocol_info` is optional for
  both protocols, so the constructor never established the non-None those
  annotations claimed. `filter_methods` got a named `HttpFilterMethod` type and
  its three literal-key lookups became subscripts, which is what removed
  `"None" not callable`. `GatewayResponse['request_tracer']` became
  `list[MutableMapping[...]]`, matching the live `ResultsCollector` views it has
  always held, and SOAP now derives `reported_collectors` from the same bind
  `logic/http_client.py` does instead of reaching for an undeclared attribute.
- **Two latent defects surfaced and were fixed.** Honest `Optional[Text]`
  annotations showed `self.remote_path` reaching `sftp.lstat()` and
  `self.server_path` reaching `aioftp.Client.stat()`, both typed
  `str | PurePath`. Absent, each arrived as `None` and raised a `TypeError`
  from inside the dependency — belonging to no transport family, so it escaped
  `request()` **un-enveloped** as a library bug instead of being reported as the
  caller's configuration error. `SFTPRequest._validate_mode` already guarded the
  identical shape for `mode`; the check is extended there and a matching
  `FTPRequest._validate_server_path` added, both running before the connect so
  nothing is opened for a call that cannot run.
- **`py.typed` added last** (FI-13), only once mypy was genuinely green, with
  `[tool.setuptools.package-data]` to carry it (setuptools does not ship a
  non-`.py` file otherwise). Verified in the real artifacts:
  `async_gateway/py.typed` in the wheel and
  `async_gateway-1.0.0/async_gateway/py.typed` in the sdist; `twine check`
  passes on both. The new `test_py_typed_ships_in_both_artifacts` derives the
  sdist prefix from the declared version so it does not rot at the next release.
- **Added a CI step that asserts the config itself.** `mypy` being green does
  not prove `ignore_errors` is absent — restoring it makes the job greener. The
  spec's own premortem names "`ignore_errors = True` restored within a week" as
  the likely failure, so a step greps for it and for a *global*
  `ignore_missing_imports` inside `[tool.mypy]`. Both arms verified against
  injected copies of `pyproject.toml`: each exits 1 naming what it caught.
- **Verify (green).** `mypy async_gateway` → `Success: no issues found in 27
  source files`. `grep -rn "ignore_errors" .` → nothing outside the spec, the
  audit report, tickets, and the two prose references named below. `flake8 .`
  → exit 0. `check_suppressions.py` → exit 0. `pytest` → **1316 passed**,
  coverage 98.25% (down from 98.41: the two new validators' raise-arms are
  uncovered — AGW-27 owns that). `python -m build` + `twine check dist/*` →
  both pass.

## Defect-loop items reported, not fixed

- **Nested multipart is unhandled** (`helpers/internal/request_helper.py:301`).
  `MultipartReader.next()` returns `MultipartReader | BodyPartReader | None`,
  and only the `BodyPartReader` arm has `read_chunk`. A nested
  `multipart/...` part therefore reaches that call and raises `AttributeError`
  today. Descending into it, or refusing it with a typed error, is a behaviour
  change and outside this story's type-conformance-only boundary — pinned with a
  justified `# type: ignore[union-attr]` naming the defect and routed to the
  story owning the multipart read path.
- **`typing.Text` (R30-AC9) not attempted.** AGW-24 flagged ~200 remaining
  sites. `Text is str`, so the substitution is semantically free but touches
  most of the tree, and doing it inside a story whose boundary is "type
  conformance only" would bury the 24 real fixes above in a rename diff.
  Recorded for a story that can own it alone.
- **Two prose references to `ignore_errors` as a live setting remain**, in
  `CLAUDE.md:296` and `.claude/rules/fastapi-patterns.md:119`. Both are on the
  project-wide list requiring explicit user approval, and **AGW-30 owns exactly
  those two files**. Left for it rather than edited here; the DoD's grep
  criterion is otherwise satisfied.
