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
- 2026-08-22 — S3A RED adds 18 deterministic signed-URL failure and cleanup
  cases without changing production: client-construction failure before
  ownership, Google service/IAM/transport generation failures, exact
  body-over-cleanup precedence, cleanup-only typed outcomes, one gateway
  generation invocation, zero breaker use, and private bearer discard. The
  exact focused run collects 504 cases: all 486 prior cases plus three new
  already-supported behaviors pass, while 15 new cases fail only in the
  missing signed-URL normalization/precedence paths. Scoped flake8 passes;
  the accepted test SHA-256 is
  `4a1ffc89af7835b9b3b6cc800f91978746bece7304266c6fcbbed6ad1264d784`.
  AGW-50 remains IN PROGRESS for S3A GREEN and the separately bounded S3B
  cancellation/timeout/bearer-surface tranche.
- 2026-08-22 — S3A GREEN normalizes recognized storage-client construction,
  signing, and cleanup failures without entering the breaker or retry path.
  Source ADC failures remain `CONFIG`; signing-stage service/IAM/transport
  failures use their safe public types; an existing body failure wins over a
  later hostile close; and a cleanup-only failure discards the private bearer
  before conversion. The immutable 504-case focused suite passes in full,
  including exactly one signing invocation and zero breaker calls. Focused
  `gcs_client.py` coverage is 809/809 statements and 272/272 branches;
  entrypoint regression passes 276/276; scoped flake8 and full-package mypy
  pass. AGW-50 remains IN PROGRESS for the separately bounded S3B
  cancellation/timeout/bearer-surface tranche.
- 2026-08-22 — S3B1 tests-first lifecycle probe adds ten deterministic public
  `request()` cases across ADC discovery, source refresh, impersonated-
  credential construction, target refresh, and storage-client construction,
  with cancellation and injected result-acceptance timeout at every seam.
  Every new row already passes the current implementation, so this tranche
  records meaningful regression evidence rather than manufacturing a RED
  failure: late provider work drains under retained capacity, a late-created
  client closes off-loop exactly once before release, the one repeated-
  cancellation adversary retains its first cancellation, and every path makes
  zero signing/breaker calls with no credential, identity, cleanup, or signed-
  URL sentinel in public/log surfaces. The exact focused GCS/no-blocking run
  passes 514/514 with production untouched. AGW-50 remains IN PROGRESS for
  the later post-generation/close and broader bearer-containment tranches.
- 2026-08-22 — S3B2 tests-first lifecycle probe adds six deterministic public
  `request()` rows after signing starts: cancellation and result-acceptance
  timeout while URL generation is blocked, plus cancellation/timeout crossed
  with recognized and unknown failures from a blocked client close after one
  private URL was generated. One close row repeats cancellation during drain.
  All six are honest passing regression evidence on the unchanged product:
  the first cancellation or `TIMEOUT`/504 wins, capacity remains retained
  until exact-once off-loop close completes, generation is invoked exactly
  once without breaker/retry, and the late bearer plus credential/cleanup
  sentinels remain absent from the live envelope, result, exception, cause,
  log, and breaker surfaces. The exact focused GCS/no-blocking run passes
  520/520. AGW-50 remains IN PROGRESS for the separately bounded successful
  bearer-surface/telemetry tranche.
- 2026-08-22 — S3B3 tests-first capacity and bearer-containment probe adds
  four deterministic public-request rows. Saturated four-permit and safely
  restored closing admission both return `GCS_CAPACITY`/503 before ADC,
  client, signing, close, or breaker execution; exact rejection telemetry is
  secret-safe and all four permits are reusable afterward. Successful GET and
  PUT each expose one realistic V4 bearer exactly once, only at
  `protocol_details.signed_url`, while the original `gs://` target, exact
  method/expiry/PUT required-header contract, one signing invocation, zero
  breaker execution, and close-before-publication remain intact. Capacity,
  drain, log, error/cause, and breaker surfaces contain no signature or
  credential-query fragment. All four rows pass on the unchanged product and
  the exact focused GCS/no-blocking run passes 524/524. Caller-configured
  postprocessors remain explicitly outside this tranche under the approved
  processor trust boundary. AGW-50 remains IN PROGRESS pending review and its
  contribution audit.
- 2026-08-22 — Contribution-audit defect-loop RED adds 12 deterministic
  tests for the three independent High findings while preserving all 524
  prior focused cases. The exact 536-case GCS/no-blocking run reports 526
  pass and 10 product-gap failures: exact-object validation has two passing
  controls and three missing prefix/glob refusals; all six malformed nominal
  signed-URL results are accepted instead of failing closed; and the one
  unknown-close case preserves exception identity but retains its realistic
  bearer in exception state and live `_signed_url_attempt` frame locals.
  Production remains byte-identical; scoped flake8, diff checks, and the
  no-live-I/O boundary pass. Accepted RED test SHA-256:
  `8721f3d482939dabac81f132d97307248711ed0cf9776ae8729e9d43a1ed9aba`.
  AGW-50 remains IN PROGRESS for the minimal GREEN defect correction.
- 2026-08-22 — Defect-loop GREEN closes the three contribution-audit Highs
  at their existing seams: signed URLs now reject trailing-prefix and frozen
  glob target shapes before breaker lookup, accept only a non-empty string
  from URL generation, and scrub a private bearer from direct exception-graph
  strings and live strategy state before any failed cleanup propagates. The
  immutable 536-case focused suite passes in full; focused `gcs_client.py`
  coverage is 826/826 statements and 280/280 branches; entrypoint regression
  passes 276/276; scoped flake8 and full-package mypy pass. Accepted RED test
  SHA-256 remains
  `8721f3d482939dabac81f132d97307248711ed0cf9776ae8729e9d43a1ed9aba`.
  AGW-50 remains IN PROGRESS pending independent contribution re-audit.
