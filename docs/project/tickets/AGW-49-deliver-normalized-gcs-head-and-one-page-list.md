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
