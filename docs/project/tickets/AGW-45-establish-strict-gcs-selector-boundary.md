# AGW-45: Establish strict GCS selector boundary

- **Status:** IN REVIEW
- **Branch:** `codex/gcs-selector-backend`
- **Story:** GCS-02 — [GCS selector story plan](../../specs/gcs_selector_stories.md)
- **Spec:** [GCS selector specification](../../specs/gcs_selector_spec.md)
- **Decisions:** Bounded GCS decisions of record in [the GCS selector specification](../../specs/gcs_selector_spec.md); no ADR is required.
- **Acceptance criteria:** AC1.1, AC1.2, AC1.3, AC1.5, AC2.1, AC2.2, AC2.3, AC2.4, AC2.5, AC2.6, AC8.5, AC11.1, AC12.1, AC12.2, AC12.3
- **Files (declared scope):** `asyncio_gateway/asyncio_gateway.py`, `asyncio_gateway/logic/__init__.py`, `asyncio_gateway/logic/gcs_client.py`, `tests/logic/test_gcs_client.py`, `tests/test_entrypoint.py`

## Why

Invalid selector, scheme, target, auth, and command options must be rejected at
their mandated boundary before any cloud or local side effect (GCS-02).

## RED-first plan and definition of done

- **RED first:** Add deterministic entrypoint/client tests for case-insensitive
  `GCS` registry resolution and exact `{'gs'}` allowlist; selector/URL-type/
  strict-info errors before envelope/preprocessor; post-preprocessor non-`gs`
  scheme guard; target/auth validation before breaker/ADC/local I/O; bucket-only
  breaker destination; fresh copied command-specific options; command, numeric,
  page-token (empty, whitespace, 4096/4097 UTF-8 bytes, encoding failure), and
  signed-method boundaries. Run them while GCS is absent/skeletal and record
  failures.
- **GREEN/refactor:** Register only GCS, add the GCS-to-`gs` dispatch row, and
  add the smallest strict `GcsRequest` boundary/validator satisfying those
  tests. Preserve existing selector behavior and top-level envelope keys.
- **Done checks:** All named boundary tests green; no ADC, path helper, breaker,
  or SDK double observed on rejected input; exactly five files or fewer; no
  public surface beyond the specification.

## Work Log

- 2026-08-22 — Opened from approved GCS-02 planning before implementation;
  recorded its five-file boundary, acceptance criteria, RED-first proof, and
  dependency relation. Files: this ticket and the local ticket/wiki index.
- 2026-08-22 — Implementation started from foundation commit `649a65c` in
  the isolated backend worktree; reloaded the requested skills and re-read the
  approved boundary clauses, story, ticket, entrypoint, base, and S3 patterns.
- 2026-08-22 — Partial RED proved the absent selector boundary with the
  venv-s24 `pytest tests/test_entrypoint.py -k 'gcs_selector'` command: 2
  failed, 274 deselected. The failures were the missing `GCS` registry entry
  and missing exact `{'gs'}` scheme row; the focused coverage threshold also
  failed as expected.
- 2026-08-22 — Partial GREEN added only the `GcsRequest` skeleton, registry
  entry, and exact `gs` scheme row. The same focused selection with
  `--no-cov` passed 2 tests with 274 deselected. Scoped flake8, three-file
  mypy, and `git diff --check` passed. Strict command/options and target/auth
  boundary work remains; status stays IN PROGRESS.
- 2026-08-22 — Accepted full RED before production validation edits:
  `tests/logic/test_gcs_client.py` collected 137 tests, with 115 failed and
  22 passed. GREEN then passed all 137 focused boundary tests in 0.24s.
  The entrypoint regression initially exposed its stale nine-protocol census;
  the essential GCS census correction then passed all 276 entrypoint tests.
  Scoped flake8, mypy, and `git diff --check` passed. No ADC, provider,
  signing, operation, capacity, push, deployment, or live-GCP work was done.
  Implementation commits: `c8281ab`, `053a836`.
- 2026-08-22 — Administrative correction after independent validation:
  restored status from `DONE` to `IN PROGRESS`. Implementation completion
  does not advance the ticket past the implementation-stage workflow gate;
  review and PR-stage transitions remain pending.
- 2026-08-23 — Formal-review docstring defect-loop RED at clean HEAD
  `7247a4a`: the unchanged entrypoint baseline passed 290/290, then three
  section-bounded public `request()` docstring rows collected 293 tests with
  290 passed and exactly 3 failed. The `protocol`, `data`, and `auth` failures
  respectively prove the missing registered-GCS, unused-data, and
  ADC/Workload-Identity claims. Test SHA-256 is
  `85aeff973e722d3f21d8795b94c93549f2f997f9099ee7b8055fc60c0e2a858f`;
  production remained unchanged. Status stays IN PROGRESS for GREEN and
  independent review.
- 2026-08-23 — Minimal docstring GREEN added only the three required GCS
  claims to the public `request()` parameter sections. The three focused rows
  passed 3/3, the complete entrypoint module passed 293/293, and the docs
  module passed 243/243. Full flake8, the CI formatting selector, and mypy over
  all 33 source files passed. Normalized ASTs with every docstring removed
  remained byte-identical at SHA-256
  `34cfdb97665a7fa9b4895dc11f22d97f1673585acc7f3c6a95a4ff03a7d94a7c`;
  the frozen test hash remained unchanged. No signature, control flow, type,
  import, secret, or runtime behavior changed. Status stays IN PROGRESS for
  independent review.
- 2026-08-23 — JOIN-2 evidence: code review Iteration 3 APPROVED at product
  HEAD `e22a440b` (C0/H0/M0/L0); build-green passed 5,927 tests with 20
  declared skips, repo-wide and changed-module 100% statement/branch coverage,
  and artifact verification; contract-clear VERIFIED (C0/H0/M0/L1), with only
  the nonblocking stale internal protocol-count docstring. Downstream tester,
  security, operability, acceptance, and PR stages remain open.
