# AGW-45: Establish strict GCS selector boundary

- **Status:** OPEN
- **Branch:** `codex/gcs-selector`
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
