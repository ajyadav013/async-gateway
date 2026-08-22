# AGW-50: Deliver constrained V4 GCS signed GET and PUT

- **Status:** IN PROGRESS
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
- 2026-08-22 — S1 RED adds seven deterministic direct-ADC cases: exact V4
  GET/PUT calls at 1/default-900/3600 seconds, PUT header/generation bounds,
  signer capability, off-loop generation/close, close-before-publication, and
  bearer-only refusal. Focused result: 471 pass and all 7 new cases fail only
  at the untouched signed-URL placeholder (`gcs_client.py:1118`). Production
  remains byte-identical; no commit was created.
- 2026-08-22 — S1 GREEN implements only direct sign-capable ADC GET/PUT:
  signing capability and identity are checked off-loop, exact V4 arguments are
  generated once without breaker execution, and the client closes before the
  bearer URL is atomically published. Focused GCS/no-blocking tests pass
  478/478; entrypoint tests pass 276/276; scoped flake8 and full-package mypy
  pass. Focused `gcs_client.py` coverage is 99.31%; the remaining blank signer
  identity case is the next direct-signing RED, impersonation belongs to S2,
  and credential/cleanup failure paths belong to S2/S3. AGW-50 remains in
  progress and the frozen S1 test hash is unchanged.
- 2026-08-22 — S2 RED adds eight deterministic authentication cases. Three
  unusable direct signer identities already pass; five impersonation cases
  fail at the missing target-credential path while all 478 prior cases remain
  green. The accepted test SHA-256 is
  `b33e55f7e711cdc062a746f5ac45167885252e8552397799a8beec091f63af0f`.
- 2026-08-22 — S2 GREEN selects direct or fixed-scope impersonated signing
  credentials after source ADC refresh, constructs and refreshes the target
  credential through the private GCS worker, and maps target refresh refusal
  to sanitized `GCS_STATUS`/502. Focused GCS/no-blocking tests pass 486/486;
  entrypoint tests pass 276/276; scoped flake8 and full-package mypy pass.
  Focused coverage has 764/764 statements and 257/258 branches; the sole
  `1311->1314` cleanup branch remains owned by S3. AGW-50 remains IN PROGRESS
  for the hostile lifecycle and bearer-containment tranche.
