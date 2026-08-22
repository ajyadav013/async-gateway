# AGW-44: Package the supported Google Storage dependency

- **Status:** OPEN
- **Branch:** `codex/gcs-selector`
- **Story:** GCS-01 — [GCS selector story plan](../../specs/gcs_selector_stories.md)
- **Spec:** [GCS selector specification](../../specs/gcs_selector_spec.md)
- **Decisions:** Bounded GCS decisions of record in [the GCS selector specification](../../specs/gcs_selector_spec.md); no ADR is required.
- **Acceptance criteria:** AC1.4, AC12.1, AC12.3, AC12.4
- **Files (declared scope):** `tests/test_packaging.py`, `pyproject.toml`

## Why

The selector cannot be imported reliably, or distributed safely, without the
frozen `google-cloud-storage>=3,<4` package contract (GCS-01; AC1.4,
AC12.1, AC12.3, AC12.4).

## RED-first plan and definition of done

- **RED first:** Add focused packaging assertions that fail until runtime
  metadata contains exactly `google-cloud-storage>=3,<4` and the clean
  package/import inventory recognizes the selector dependency; run the focused
  packaging test and capture the failure.
- **GREEN/refactor:** Add only the approved metadata entry; keep no emulator or
  extra dependency. Re-run the focused packaging test, then the relevant
  install/import/build checks prescribed by the specification.
- **Done checks:** Exact dependency range present; no new dependency; focused
  packaging assertion green; file count remains 2.

## Work Log

- 2026-08-22 — Opened from approved GCS-01 planning before implementation;
  recorded the frozen two-file boundary, acceptance criteria, RED-first proof,
  and dependency relation. Files: this ticket and the local ticket/wiki index.
