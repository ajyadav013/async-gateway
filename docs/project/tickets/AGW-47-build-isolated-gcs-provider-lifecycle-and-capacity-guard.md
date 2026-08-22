# AGW-47: Build isolated GCS provider lifecycle and capacity guard

- **Status:** IN REVIEW
- **Branch:** `codex/gcs-selector-backend`
- **Story:** GCS-04 — [GCS selector story plan](../../specs/gcs_selector_stories.md)
- **Spec:** [GCS selector specification](../../specs/gcs_selector_spec.md)
- **Decisions:** Bounded GCS decisions of record in [the GCS selector specification](../../specs/gcs_selector_spec.md); no ADR is required.
- **Acceptance criteria:** AC3.1, AC3.2, AC3.3, AC3.4, AC3.5, AC3.6, AC8.2, AC8.3, AC8.4, AC8.5, AC8.6, AC11.1, AC12.1, AC12.2, AC12.3, AC13.1, AC13.2, AC13.3, AC13.4, AC13.5, AC13.6, AC13.7, AC13.8
- **Files (declared scope):** `asyncio_gateway/logic/gcs_client.py`, `tests/logic/test_gcs_client.py`, `tests/test_no_blocking_io.py`

## Why

Google SDK, ADC, credential, IAM, and client work must not block or consume the
default executor, and stalled work must not starve unrelated protocols (GCS-04).

## RED-first plan and definition of done

- **RED first:** Add deterministic executor/lease doubles and focused contract
  tests proving exact private executor identity/configuration; no provider seam
  or default-executor submission; nonblocking four-lease admission and prompt
  fifth/closing `GCS_CAPACITY`; one provider future per retained lease; finite
  SDK timeout and result-acceptance deadline; shielded drain/resource close;
  safe telemetry; idempotent deferred shutdown; and structural service/
  transport retry/breaker classification. These tests permit unchanged
  `read_guarded_file` and `stream_to_path` internal default-executor use.
- **GREEN/refactor:** Implement private lifecycle helpers, never a shared
  offloader or public tuning surface. Verify no provider result becomes success
  after deadline, cleanup never replaces original failure/cancellation, and no
  operational SDK retry will be enabled by later command stories.
- **Done checks:** Focused lifecycle/AST tests green; thread identity captured
  for every provider seam; blocked four-worker test still schedules unrelated
  default-executor work; all secrets absent from telemetry; three-file limit met.

## Work Log

- 2026-08-22 — Documentation defect final candidate is GREEN after wrapping
  only the two overlong docstring lines without changing their wording. The
  isolated docstring node reports 1 passed, scoped flake8 reports no findings,
  and the stripped AST remains identical before and after with SHA-256
  `0ca2b189ca9899ad0a5f14a8cc51ed574ecdd911ec7b301afd9adfc158093259`.
  `git diff --check`, ticket-index JSON parsing, and the exact allowed scope
  check also pass. The candidate remains uncommitted for independent review.
- 2026-08-22 — Documentation defect RED reproduced exactly: the isolated
  `gcs_client.py` docstring node failed with seven missing-section findings.
  The documentation-only candidate clears that node (1 passed) and all 536
  focused GCS/no-blocking tests; scoped mypy, diff, JSON, scope, and stripped-
  AST equivalence checks pass. Scoped flake8 remains RED on two overlong
  docstring lines, so the candidate is intentionally uncommitted pending its
  bounded correction and independent review.
- 2026-08-22 — Tranche B shutdown and telemetry lifecycle is GREEN: the exact
  focused command (`pytest tests/logic/test_gcs_client.py
  tests/test_no_blocking_io.py --no-cov -q`) reports 198 passed in 0.32s.
  Admission closing and active-lease accounting are atomic, shutdown is
  immediate when idle or deferred through the final idempotent release, and
  exact capacity/drain events remain free of request and provider data. Scoped
  flake8, scoped mypy, `git diff --check`, and ticket-index JSON validation all
  pass. Status remains IN PROGRESS because real command-owned client lifecycle
  belongs to GCS-05/GCS-06/GCS-07.
- 2026-08-22 — Tranche B RED captured without production edits: the exact
  focused command collected 198 tests, with 193 passed and five expected
  failures for the absent shutdown and capacity/drain telemetry behavior.
  Eight cases were added; the three AST/default-executor guard cases already
  pass against tranche A. Scoped flake8 and `git diff --check` pass. Original
  cancellation and acceptance-timeout precedence over hostile late-result
  cleanup are covered through the existing lease seam. Full body/service/
  transport failure precedence over an owned client-close failure remains
  acceptance-required and is intentionally deferred to GCS-05/GCS-06/GCS-07,
  where the first real operation-owned client lifecycle exists.
- 2026-08-22 — Tranche A lifecycle/mapping foundation is GREEN: the exact
  focused command (`pytest tests/logic/test_gcs_client.py
  tests/test_no_blocking_io.py --no-cov -q`) reports 190 passed in 0.30s;
  scoped flake8, scoped mypy, and `git diff --check` also pass. Status remains
  IN PROGRESS because shutdown, telemetry/AST guards, and command operations
  are intentionally deferred to their authorized follow-on tranches.
- 2026-08-22 — Marked IN PROGRESS for the tests-only RED phase on
  `codex/gcs-selector-backend`; production remains unchanged while focused
  lifecycle, capacity, isolation, normalization, and AST contracts are added.
- 2026-08-22 — Opened from approved GCS-04 planning before implementation;
  recorded its three-file boundary, acceptance criteria, RED-first proof, and
  dependency relation. Files: this ticket and the local ticket/wiki index.
- 2026-08-23 — JOIN-2 evidence: code review Iteration 3 APPROVED at product
  HEAD `e22a440b` (C0/H0/M0/L0); build-green passed 5,927 tests with 20
  declared skips, repo-wide and changed-module 100% statement/branch coverage,
  and artifact verification; contract-clear VERIFIED (C0/H0/M0/L1), with only
  the nonblocking stale internal protocol-count docstring. Downstream tester,
  security, operability, acceptance, and PR stages remain open.
