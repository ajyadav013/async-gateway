# AGW-46: Introduce stable GCS error vocabulary

- **Status:** OPEN
- **Branch:** `codex/gcs-selector`
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
