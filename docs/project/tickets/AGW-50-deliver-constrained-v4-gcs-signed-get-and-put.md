# AGW-50: Deliver constrained V4 GCS signed GET and PUT

- **Status:** OPEN
- **Branch:** `codex/gcs-selector`
- **Story:** GCS-07 — [GCS selector story plan](../../specs/gcs_selector_stories.md)
- **Spec:** [GCS selector specification](../../specs/gcs_selector_spec.md)
- **Decisions:** Bounded GCS decisions of record in [the GCS selector specification](../../specs/gcs_selector_spec.md); no ADR is required.
- **Acceptance criteria:** AC3.1, AC3.2, AC3.4, AC9.1, AC9.2, AC9.3, AC9.4, AC9.5, AC10.1, AC10.2, AC10.3, AC10.4, AC10.5, AC11.1, AC11.2, AC11.3, AC11.4, AC11.5, AC12.1, AC12.2
- **Files (declared scope):** `asyncio_gateway/logic/gcs_client.py`, `tests/logic/test_gcs_client.py`

## Why

A signed URL is a bearer secret; issuance must have no gateway retry/breaker
path and must not leak an unpublished URL or credentials (GCS-07).

## RED-first plan and definition of done

- **RED first:** Add deterministic direct-signing and IAM-impersonation tests
  for strict GET/PUT/method/expiry/service-account/content-type/PUT-cap
  validation; signer capability checks; exact `timedelta(seconds=value)` calls
  at 1/900/3600; one `Blob.generate_signed_url` gateway invocation with client
  open; GET without arbitrary headers/query; PUT dedicated `content_type`,
  exact two SDK headers, and separate exact three public `required_headers`; no
  breaker/gateway retry while allowing credential-internal IAM retry; shielded
  close before atomic publication; and sentinel URL/credential absence in all
  failure, cancellation, log, trace, exception, and breaker surfaces.
- **GREEN/refactor:** Implement signed GET/PUT only through the existing
  lifecycle. Keep the URL in private local state until close succeeds; discard
  it if close fails or cancellation occurs before publication.
- **Done checks:** Focused signing and containment suite green; every successful
  detail schema exact; no custom host/query/header/credential input; two-file
  limit met.

## Work Log

- 2026-08-22 — Opened from approved GCS-07 planning before implementation;
  recorded its two-file boundary, acceptance criteria, RED-first proof, and
  dependency relation. Files: this ticket and the local ticket/wiki index.
