# AGW-49: Deliver normalized GCS head and one-page list

- **Status:** IN PROGRESS
- **Branch:** `codex/gcs-selector`
- **Story:** GCS-06 — [GCS selector story plan](../../specs/gcs_selector_stories.md)
- **Spec:** [GCS selector specification](../../specs/gcs_selector_spec.md)
- **Decisions:** Bounded GCS decisions of record in [the GCS selector specification](../../specs/gcs_selector_spec.md); no ADR is required.
- **Acceptance criteria:** AC3.1, AC3.3, AC3.4, AC6.1, AC6.2, AC6.3, AC7.1, AC7.2, AC7.3, AC7.4, AC8.2, AC8.3, AC8.4, AC8.6, AC11.1, AC11.2, AC11.4, AC12.1, AC12.2
- **Files (declared scope):** `asyncio_gateway/logic/gcs_client.py`, `tests/logic/test_gcs_client.py`

## Why

Consumers need stable metadata and pagination without receiving SDK types,
recursively enumerating a bucket, or disclosing caller tokens (GCS-06).

## RED-first plan and definition of done

- **RED first:** Add focused head/list tests for one off-loop `retry=None`
  finite-timeout metadata fetch; exact normalized head schema/type refusal; one
  page construction/fetch/iteration/close only; exact unchanged caller token
  reaching the SDK (including non-empty whitespace and 4096-byte token);
  service-order items, exact list schema, no recursive next-page following,
  malformed item/page failure, and no caller-token echo/logging.
- **GREEN/refactor:** Implement only head and a single bounded list page on the
  existing private lifecycle. Normalize every declared scalar/mapping; do not
  add pagination APIs.
- **Done checks:** Focused tests green; `max_items` remains 1..1000; list prefix
  accepts empty target only for list; all returned schemas contain no SDK/page/
  iterator object; two-file limit met.

## Work Log

- 2026-08-22 — Opened from approved GCS-06 planning before implementation;
  recorded its two-file boundary, acceptance criteria, RED-first proof, and
  dependency relation. Files: this ticket and the local ticket/wiki index.
- 2026-08-22 — Began the head/list foundation RED tranche with deterministic
  public-path SDK doubles for exact metadata reload, one-page iteration,
  opaque-token forwarding, service-order normalization, retained lease, and
  off-loop cleanup behavior. Files: `tests/logic/test_gcs_client.py`, this
  ticket, and `docs/project/tickets/index.json`.
- 2026-08-22 — Reproduced the foundation RED: 328 collected, 322 existing
  cases passed, and six new cases failed only at the intentionally absent
  `head`/`list` dispatch (`NotImplementedError`). Production stayed untouched;
  no collection, fixture, network, sleep, or timing-oracle failure occurred.
  Files: `tests/logic/test_gcs_client.py`, this ticket, and
  `docs/project/tickets/index.json`.
- 2026-08-22 — Implemented the head/list foundation on the retained private
  lease: one exact off-loop head reload or one exact bounded list-page fetch,
  closed JSON-safe success schemas, opaque caller-token forwarding, and owned
  client cleanup before publication. The accepted 328-case suite, 276
  entrypoint cases, scoped flake8, and scoped mypy pass. Files:
  `asyncio_gateway/logic/gcs_client.py`, this ticket, and
  `docs/project/tickets/index.json`.
- 2026-08-22 — Added the normalization-hardening RED matrix for every frozen
  head/list scalar family, metadata mapping copies, malformed items/pages,
  server-token coherence, whole-page atomicity, hostile-value containment, and
  the `max_items` bound. The exact focused suite collects 380 cases: the 328
  baseline plus 52 new cases, with 371 passing and nine genuine product gaps
  (six accepted empty required-meaning scalars, one oversized page, and two
  malformed page/iterator exceptions escaping instead of `GCS_STATUS`/502).
  Production stayed untouched; collection, fixtures, network, sleeps, timing
  oracles, scoped flake8, and diff checks are clean. Files:
  `tests/logic/test_gcs_client.py`, this ticket, and
  `docs/project/tickets/index.json`.
- 2026-08-22 — Closed the nine normalization gaps by rejecting empty optional
  service scalars, refusing pages larger than `max_items` before publication,
  and translating only malformed iterator/page shape access into sanitized
  `GCS_STATUS`/502. The immutable focused suite passes 380 cases and the
  entrypoint suite passes 276; scoped flake8, mypy, diff, JSON, and exact-scope
  checks pass. Whitespace-only opaque server tokens remain valid. Files:
  `asyncio_gateway/logic/gcs_client.py`, `tests/logic/test_gcs_client.py`, this
  ticket, and `docs/project/tickets/index.json`.
- 2026-08-22 — Added 63 deterministic public head/list error-hardening cases
  for ADC/refresh, transport at every command seam, the complete service-status
  retry matrix, body-over-cleanup precedence, cleanup-only typing, programming-
  defect identity, and caller/server/provider secret containment. All 443
  focused cases pass, so this tranche found no genuine product gap and made no
  production change. Files: `tests/logic/test_gcs_client.py`, this ticket, and
  `docs/project/tickets/index.json`.
- 2026-08-22 — Independent validation reproduced all 443 focused passes and
  confirmed scoped flake8, diff, JSON, production/config isolation, and the
  exact three-file test/evidence scope. The passing tranche remains tests-only;
  AGW-49 stays IN PROGRESS. Files: `tests/logic/test_gcs_client.py`, this
  ticket, and `docs/project/tickets/index.json`.
- 2026-08-22 — Added eight event-driven retained-lifecycle cases spanning head
  reload and list construction/page-fetch/iteration under repeated cancellation
  or injected result-acceptance expiry. The exact suite now collects 451 cases:
  447 pass and four timeout cases expose one genuine precedence gap where a
  cancellation delivered during hostile client cleanup replaces the earlier
  accepted `TIMEOUT`. Three repeated focused runs reproduce four pass/four fail
  with no sleep, micro-timeout, collection, fixture, or network failure.
  Production/config stayed untouched and the ticket remains IN PROGRESS. Files:
  `tests/logic/test_gcs_client.py`, this ticket, and
  `docs/project/tickets/index.json`.
- 2026-08-22 — Preserved the first accepted head/list body outcome through
  retained client cleanup: a known result-acceptance timeout now wins over a
  later cleanup cancellation, while body-first cancellation and cleanup-only
  typing retain their existing behavior. The immutable lifecycle suite passes
  all 451 cases, the entrypoint suite passes 276, and scoped flake8 plus
  full-package mypy pass. Focused branch coverage for `gcs_client.py` is
  95.64% combined (96.36% statements, 93.55% branches; 26 missing lines and
  16 missing branches), so the residual gaps are reported for a later
  tests-only tranche rather than changing accepted tests in GREEN. Status
  remains IN PROGRESS. Files: `asyncio_gateway/logic/gcs_client.py`,
  `tests/logic/test_gcs_client.py`, this ticket, and
  `docs/project/tickets/index.json`.
- 2026-08-22 — Added the accepted deterministic coverage tranche without
  changing its frozen test content, then removed the unreachable provider-
  drain fallback: after retained cancellation is handled, the lifecycle state
  can only represent result-acceptance timeout. All 471 focused GCS/blocking-
  I/O cases pass; focused `gcs_client.py` coverage now has only the untouched
  future signed-URL placeholder missing (710/711 statements and 245/246
  branches). The ticket remains IN PROGRESS pending its contribution audit and
  AGW-50 owns replacement and coverage of that placeholder. Files:
  `asyncio_gateway/logic/gcs_client.py`, `tests/logic/test_gcs_client.py`, this
  ticket, and `docs/project/tickets/index.json`.
- 2026-08-23 — Justified the eight intentional GCS type suppressions required
  by the repository policy: the untyped GCS SDK import and malformed/foreign
  provider values injected by normalization tests. The suppression checker,
  all 522 GCS tests, full flake8, CI-format flake8, and package mypy pass;
  executable suppression counts/categories and both Python ASTs are unchanged.
  AGW-49 remains IN PROGRESS for independent formal-review recheck. Files:
  `asyncio_gateway/logic/gcs_client.py`, `tests/logic/test_gcs_client.py`, this
  ticket, and `docs/project/tickets/index.json`.
