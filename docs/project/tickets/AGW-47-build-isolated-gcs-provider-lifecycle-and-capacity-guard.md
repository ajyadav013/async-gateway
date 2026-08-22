# AGW-47: Build isolated GCS provider lifecycle and capacity guard

- **Status:** IN PROGRESS
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
