# AGW-47: Build isolated GCS provider lifecycle and capacity guard

- **Status:** IN REVIEW
- **Branch:** `codex/gcs-selector`
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
- 2026-08-23 — Pipeline Green cancellation-oracle defect loop at frozen
  product HEAD `362e184f`: the unchanged test first reproduced Python 3.10 as
  507 passed / 21 failed and Python 3.11 as 525 passed / 3 failed. Replacing
  exactly ten caller-boundary `.args` checks with the repository's established
  cross-version cancellation helper made Python 3.10 fully green at 528/528,
  retained exactly the three accepted Python 3.11 product RED nodes at 525
  passed / 3 failed, and kept Python 3.14 green at 528/528. The internal
  pre-boundary cancellation assertion and all lifecycle, capacity, status,
  identity, context, and bearer-containment assertions remain intact. Test
  SHA-256 is `4fa758b51d2217b20d565c46e1bff4835b81562d2f1f59f0c75ec0ab9dce4e43`;
  frozen `gcs_client.py` SHA-256 is
  `a0cfcac7c306554f4c2548bbb28366f56e6aa16c1c48f562680498b924e248ef`.
  The candidate remains uncommitted and intentionally RED on Python 3.11 for
  the separate production normalization defect loop.
- 2026-08-23 — The normalization RED extension adds exactly one focused
  retained-lease regression. Its selected node fails 0 passed / 1 failed on
  both Python 3.10 and Python 3.14 because the repeated top-level cancellation
  is propagated instead of the exact earliest contiguous cancellation. The
  full Python 3.11 GCS module reports 525 passed / 4 failed: that new focused
  node plus exactly the previously accepted head, list, and signed-URL-close
  nodes. The regression also freezes cycle, non-cancellation barrier, cause,
  and exception-state preservation behavior. Scoped flake8, `py_compile`,
  diff, JSON, and exact three-file scope checks pass. Final test SHA-256 is
  `377036299c08269774038ce982706edc0d11b8fa3306750750c710ca3c35e24c`;
  production remains frozen at SHA-256
  `a0cfcac7c306554f4c2548bbb28366f56e6aa16c1c48f562680498b924e248ef`.
  The candidate remains uncommitted and intentionally RED for production
  normalization.
- 2026-08-23 — RED-review Iteration 1 corrected only the traceback oracle so
  both deliberately raised cancellation objects may gain propagation prefixes
  while retaining their original traceback tails; barrier, hidden-context,
  and cause tracebacks still require exact identity. The selected node remains
  intentionally 0 passed / 1 failed on Python 3.10, 3.11, and 3.14 solely at
  the final earliest-object identity assertion. Full Python 3.11 remains 525
  passed / 4 failed with exactly that node plus the prior head, list, and
  signed-URL-close failures. Static, JSON, diff, and three-file scope checks
  pass. Corrected test SHA-256 is
  `e7492a7eed8cfaca25c24da97e93ecf009622057bdd46b23ea8b98c4ef1c5706`;
  production remains frozen at SHA-256
  `a0cfcac7c306554f4c2548bbb28366f56e6aa16c1c48f562680498b924e248ef`.
- 2026-08-23 — Minimal production GREEN adds one private, typed, cycle-safe
  selector that follows only contiguous `CancelledError.__context__` links to
  the earliest cancellation. `_GcsLease.run()` uses it at initial capture and
  `_drain_provider_future()` uses it when first recording cancellation; no
  cause traversal, exception mutation, version branch, task internals, or
  lifecycle-loop change was introduced. The full GCS module passes 529/529 on
  Python 3.10.18, 3.11.15, 3.12.14, 3.13.6, and 3.14.7. The focused GCS plus
  no-blocking-I/O slice passes 549/549, and the entrypoint plus invariant slice
  passes 2,965/2,965. Full-suite coverage is 4,902/4,902 statements and
  1,548/1,548 branches; `gcs_client.py` is 829/829 statements and 282/282
  branches. Scoped Flake8, the exact CI-format selector, mypy over 33 source
  files, suppression policy, `py_compile`, and diff checks pass. The immutable
  test SHA-256 remains
  `e7492a7eed8cfaca25c24da97e93ecf009622057bdd46b23ea8b98c4ef1c5706`;
  candidate production SHA-256 is
  `54d91f4180d3589be9177ead1e8bb861241153b171ea9bc7572e6756d28a341b`.
  The candidate remains uncommitted for independent review.
