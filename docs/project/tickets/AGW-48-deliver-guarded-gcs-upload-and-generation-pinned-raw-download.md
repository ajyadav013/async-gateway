# AGW-48: Deliver guarded GCS upload and generation-pinned raw download

- **Status:** OPEN
- **Branch:** `codex/gcs-selector`
- **Story:** GCS-05 — [GCS selector story plan](../../specs/gcs_selector_stories.md)
- **Spec:** [GCS selector specification](../../specs/gcs_selector_spec.md)
- **Decisions:** Bounded GCS decisions of record in [the GCS selector specification](../../specs/gcs_selector_spec.md); no ADR is required.
- **Acceptance criteria:** AC3.3, AC3.4, AC3.5, AC4.1, AC4.2, AC4.3, AC4.4, AC4.5, AC4.6, AC4.7, AC4.8, AC5.1, AC5.2, AC5.3, AC5.4, AC8.3, AC8.4, AC8.6, AC11.1, AC11.2, AC12.1, AC12.2
- **Files (declared scope):** `asyncio_gateway/logic/gcs_client.py`, `tests/logic/test_gcs_client.py`

## Why

Transfers carry the largest data-integrity risk: uploads must not reopen/race
local files, and downloads must not mix generations or transparently decompress
beyond the cap (GCS-05).

## RED-first plan and definition of done

- **RED first:** Add focused upload/download tests for one guarded pre-breaker
  read with replayed bytes/precondition; reload pin validation and caller
  generation authority; 64-KiB sequential inclusive `download_as_bytes` calls
  using pinned generation, `raw_download=True`, `retry=None`, and finite
  timeout; empty/one-byte/exact-64-KiB/multi-range cases; stored compressed
  bytes; advertised cap before path mutation; post-pin retry at zero without
  reload/repin; 404/412 abort; malformed/non-bytes/short/overlong/
  decompression-shaped range refusals; atomic destination behavior; and exact
  success detail schemas.
- **GREEN/refactor:** Reuse—not modify—`read_guarded_file` and `stream_to_path`
  while retaining the lease. Disable SDK retries at every operational call and
  preserve original cancellation/atomic-writer semantics.
- **Done checks:** Exact-call doubles green; existing target survives every
  pre-commit failure; success file equals stored/raw bytes; no checksum-
  verification claim; two-file limit met.

## Work Log

- 2026-08-22 — Opened from approved GCS-05 planning before implementation;
  recorded its two-file boundary, acceptance criteria, RED-first proof, and
  dependency relation. Files: this ticket and the local ticket/wiki index.
