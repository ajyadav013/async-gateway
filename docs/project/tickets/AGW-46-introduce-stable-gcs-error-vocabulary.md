# AGW-46: Introduce stable GCS error vocabulary

- **Status:** IN PROGRESS
- **Branch:** `codex/gcs-selector-backend`
- **Story:** GCS-03 — [GCS selector story plan](../../specs/gcs_selector_stories.md)
- **Spec:** [GCS selector specification](../../specs/gcs_selector_spec.md)
- **Decisions:** Bounded GCS decisions of record in [the GCS selector specification](../../specs/gcs_selector_spec.md); no ADR is required.
- **Acceptance criteria:** AC8.1, AC13.4, AC12.1, AC12.3
- **Files (declared scope):** `asyncio_gateway/utils/exceptions.py`, `asyncio_gateway/utils/status_map.py`, `tests/test_exceptions.py`

## Why

Later lifecycle and service behavior needs stable, safe `GCS_STATUS` and
`GCS_CAPACITY` envelope vocabulary without leaking provider objects (GCS-03).

## RED-first plan and definition of done

- **RED first:** Add focused hierarchy/status tests that fail until
  `GcsStatusError(ProtocolError)` supplies `GCS_STATUS`/502 and warning-class
  remote mapping, and `GcsCapacityError(AsyncGatewayError)` supplies
  `GCS_CAPACITY`/503 with local error-level classification and safe
  constant-only details.
- **GREEN/refactor:** Implement only those two types and map entries. Do not
  introduce conversion policy, capacity admission, or new error surfaces.
- **Done checks:** Focused exception tests green; no provider text/target/secret
  belongs to capacity error; three-file limit met.

## Work Log

- 2026-08-22 — Opened from approved GCS-03 planning before implementation;
  recorded its three-file boundary, acceptance criteria, RED-first proof, and
  dependency relation. Files: this ticket and the local ticket/wiki index.
- 2026-08-22 — Implementation started in the isolated backend worktree at
  planning commit `057bc367`; loaded the approved specification, story plan,
  repository rules, and requested implementation skills before the RED step.
- 2026-08-22 — RED: `pytest tests/test_exceptions.py -k 'gcs_status_error or
  gcs_capacity_error' -q --no-cov` failed 2 tests because both error classes
  were absent. GREEN: after adding only the two typed errors and their central
  status classifications, the same command passed 2 tests with 67 deselected;
  the complete exception module passed all 69 tests without coverage.
