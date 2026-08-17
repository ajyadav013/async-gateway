# AGW-25: flake8 to zero

- **Status:** DONE
- **Story:** S25 — spec Step 24, size M (`docs/specs/v1_release_stories.md` §4, Phase 6)
- **Spec:** `docs/specs/v1_release_spec.md` — R27-AC2,3,7,8 · R27-AC4 *(verifies AGW-6)* (Group M — Quality gates turned on)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** none
- **Files (declared scope):** ~`pyproject.toml`/`.flake8`, `.github/workflows/ci.yml` · ~the files flake8 reports (L15's five: `base.py`, `filters_helper.py`, `circuit_breaker_helper.py`, `ftp_client.py`, `sftp_client.py`) — **lint conformance only, no behaviour change**

## Why

R27 requires the linter to run, to pass, and not to be configured to see nothing — today
`application_import_names` names an illegal identifier, so import-order checking has been checking a
fiction, and the baseline suppression file hides the rest. This step drives flake8 to zero, deletes
the baseline rather than growing it, and adds the CI check that fails an unjustified `# noqa` or
`# type: ignore`. The `A005` rename is **not** performed here — AGW-6 did it under FI-15; this step
verifies it with no suppression anywhere.

Closes findings: H19, L15. Verifies FI-15 (performed at AGW-6).

## Definition of Done

- fresh venv `pip install -e '.[dev]' && flake8 .` exits 0 with no output
- `application_import_names = async_gateway` (underscore) — today an illegal identifier, so import-order checking has been checking a fiction
- one quote style chosen and formatter-enforced so the 938 quote findings become a formatter concern, not a lint backlog
- the baseline suppression file shrinks to zero and is **deleted**
- **the `A005` rename is NOT here — AGW-6 performed it; this step verifies:** `flake8` reports zero `A005` with **no `# noqa` anywhere**
- a CI step greps for bare `# noqa`/`# type: ignore` without a rule code or a `--` justification and **fails**
- a formatter runs in CI in check mode

## Dependencies

- **blockedBy:** AGW-24
- **blocks:** AGW-26

## Decisions

- **`.claude/` is excluded from flake8** (OQ15, human-decided). It is the agent
  harness — rules, skills, hooks and their bundled scripts — checked in beside the
  library rather than part of it: nothing under it is imported by `async_gateway`,
  shipped in either artifact, or executed by the suite, and it follows its own
  upstream conventions. `flake8 .` is the command `CLAUDE.md` documents and CI runs,
  so without the exclusion the documented command reports on ~745 files no story in
  this release owns.
- **The five `A003` findings are excluded per-file, not renamed.** Each is a method
  whose name is chosen by a third-party library, so a rename does not rename the
  call site inside the dependency — the override simply stops overriding. Verified
  against the installed packages rather than assumed: `aioftp.client` calls
  `self.path_io.list(src)` and `self.path_io.open(..., mode=...)`, and
  `asyncssh.sftp` calls `self._srcfs.open(...)` / `self._dstfs.open(...)`. Renaming
  `ContainedPathIO.list` or `ContainedLocalFS.open` therefore *disables path
  containment* — a security regression traded for a lint code. `GatewayError.type`
  is a wire-stable envelope key (R8) that consumers branch on. The two test fixtures
  must present the names the real clients dispatch on or they are not doubles.
  Scoped `per-file-ignores` keyed to file **and** code, so a new `list`/`open`/`type`
  class attribute anywhere else is still a finding.
- **`# noqa` was not used for the A003s.** Five comments each restating one shared
  reason is worse than the single place in `.flake8` that states it, and the DoD's
  "no `# noqa` anywhere" for `A005` is satisfied either way.
- **The justification check reads the comment block above the line, not only the
  line.** At 79 columns a real explanation does not fit beside the code, and a
  trailing-comment-only rule buys a one-word alibi instead of a reason. To keep the
  allowance honest, the nearby comment must *name the same code*.
- **No autoformatter was added.** `CLAUDE.md` records that this project configures
  none; `flake8` is the formatting judge, and the check-mode step narrows it to the
  formatting codes (`E1,E2,E3,E501,W2,W3,W505,Q`) so a formatting regression is
  named as one in the CI job list.

## Work Log

- **Measured the starting state.** `flake8 async_gateway tests examples` reported 14
  findings under the old config; correcting `application_import_names` to the legal
  `async_gateway` raised it to 42, because 28 `I100` findings had been invisible
  while first-party imports were being classified third-party. That is H19's real
  size, and it is why the setting is the first thing this story fixed.
- **Chose the import-order style by measurement, not preference.** All seven
  `flake8-import-order` styles were run against the tree: `cryptography` (the
  plugin's default) scored lowest at 34 findings versus 38–93 for the rest, and it is
  the paragraph-per-package layout the codebase already mostly follows. Kept as the
  default rather than pinned, since pinning the default adds a line that says
  nothing.
- **Normalised the import block in 29 files** (9 source, 20 test) to that style:
  stdlib paragraph, one paragraph per third-party top-level package, then the
  application paragraph. Mechanical and comment-preserving — a script refused any
  file with a non-import line inside the block rather than relocating it.
- **Fixed the three ordinary findings by hand:** `base.py:23` `E501` (the
  `CircuitBreakerHelper` import wrapped in parentheses), `base.py:160` `D401`
  ("Initializing" → "Initialize").
- **Stated the quote style explicitly** (`inline-quotes = single`, plus the
  multiline/docstring/`avoid-escape` settings). It matches the plugin's default and
  the tree, but a default is not a decision: written down, `Q000` is the mechanical
  enforcement and the audit's ~938 quote findings cannot return by a plugin default
  changing underneath the project. `flake8 --select=Q .` is at zero.
- **Wrote `.github/scripts/check_suppressions.py`** and wired it into CI. It rejects
  a bare `# noqa` / `# type: ignore`, and a coded one with no `-- why`. Verified
  against all four shapes: the two bare forms and the coded-but-unjustified form each
  exit 1 with a named reason; the justified form exits 0.
- **Annotated the 23 pre-existing suppressions the new check surfaced.** Twenty were
  the same shape — a stdlib function replaced for the length of one test to observe
  which thread the real call runs on, restored in a `finally`. Two are genuine aioftp
  stub gaps in `contained_io.py` (`_open`'s widened override, which aioftp's own
  `AsyncPathIO._open` carries the identical ignore for; and the `io.BytesIO` return
  the builtin does not actually produce). One was truly bare —
  `test_http_file_config.py:404`, where the positional call *is* the thing under
  test — and now carries `[misc]` and its reason. No suppression was added to make
  a gate pass.
- **Verify (green).** `flake8 .` → exit 0, no output. `flake8
  --select=E1,E2,E3,E501,W2,W3,W505,Q .` → exit 0.
  `python .github/scripts/check_suppressions.py` → exit 0. `pytest` → 1314 passed, 1
  skipped, coverage 98.41% (unchanged from before the story, as a lint-only change
  should be).
- **No baseline suppression file existed** to shrink or delete; the DoD line is
  discharged as already-satisfied rather than performed.
- **`A005` verified, not performed:** `flake8 --select=A005 .` reports nothing, with
  no `# noqa` anywhere in the tree. FI-15 confirmed at AGW-6.
