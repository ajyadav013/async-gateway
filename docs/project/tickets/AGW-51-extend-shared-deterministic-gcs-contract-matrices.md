# AGW-51: Extend shared deterministic GCS contract matrices

- **Status:** IN PROGRESS
- **Branch:** `codex/gcs-selector`
- **Story:** GCS-08 — [GCS selector story plan](../../specs/gcs_selector_stories.md)
- **Spec:** [GCS selector specification](../../specs/gcs_selector_spec.md)
- **Decisions:** Bounded GCS decisions of record in [the GCS selector specification](../../specs/gcs_selector_spec.md); no ADR is required.
- **Acceptance criteria:** AC1.1, AC1.4, AC8.1, AC11.1, AC12.1, AC12.2, AC12.3, AC13.4, AC13.7, AC13.8
- **Files (declared scope):** `tests/fixtures/protocol_transports.py`, `tests/test_entrypoint.py`, `tests/test_exceptions.py`, `tests/test_no_blocking_io.py`

## Why

The focused suite alone cannot prove that the selector preserves the
repository-wide protocol matrix and shared error/fixture contracts (GCS-08).

## RED-first plan and definition of done

- **RED first:** Add or extend global fixture and invariant assertions for GCS
  registry/scheme/envelope rows; exception/status rows; provider-prefix AST/
  default-executor isolation; and capacity status plus exact safe telemetry.
  Run these tests against the completed implementation to identify any contract
  mismatch before altering shared test data.
- **GREEN/refactor:** Make the minimal fixture/assertion updates that accurately
  encode the already implemented public contract. This story adds no runtime
  behavior and may not “fix” a red invariant by weakening it.
- **Done checks:** Cross-module matrices green alongside focused GCS suite;
  existing selectors unchanged; four-file limit met.

## Work Log

- 2026-08-22 — Opened from approved GCS-08 planning before implementation;
  recorded its four-file boundary, acceptance criteria, RED-first proof, and
  dependency relation. Files: this ticket and the local ticket/wiki index.
- 2026-08-22 — Reproduced the immutable global-contract RED: the unchanged
  entrypoint invariant suite reported 2395 passed and one failure because GCS
  was present only in the production registry. Began the minimal deterministic
  fixture integration without runtime changes. Files:
  `tests/fixtures/protocol_transports.py`, `tests/test_entrypoint.py`, this
  ticket, and the local ticket index.
- 2026-08-22 — Added the no-network/no-ADC GCS head, service-failure,
  provider-fault, and raw-download fixture rows and removed the temporary GCS
  exception from the exact registry assertion. The unchanged global invariant
  suite passed 2662 tests; shared envelope matrices passed 406 with 18 declared
  skips; entrypoint/exception/no-blocking modules passed 379; and focused GCS/
  no-blocking tests passed 536. Existing exception/status and provider-prefix/
  default-executor tests already covered AGW-51, so those files needed no
  duplicate assertions. Scoped flake8, `git diff --check`, ticket JSON parsing,
  and the exact four-file product boundary passed. Files:
  `tests/fixtures/protocol_transports.py`, `tests/test_entrypoint.py`, this
  ticket, and the local ticket index.
