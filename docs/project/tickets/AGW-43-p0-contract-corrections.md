# AGW-43: P0 public contract corrections

- **Status:** DONE
- **Branch:** `fix/p0-contract-corrections`
- **Story:** Work Package 1 — P0 contract corrections
- **Spec:** `docs/specs/p0_contract_corrections_spec.md`
- **Design:** `docs/specs/p0_contract_corrections_spec.md` — implementation map
- **Decisions:** `docs/decisions/0002-processors-do-not-control-dispatch.md`
- **Files (declared scope):** entry point, protocol boundary, SFTP auth/client,
  focused tests, PEP 621 metadata, README/changelog/security policy, and ticket
  ledger

## Why

The 1.0.0 public boundary silently ignores unknown keywords, silently discards
pre-processor URL edits, and couples SFTP authentication to an FTP password
validator. These behaviors can misroute caller intent or reactivate ambient SSH
credentials. The package metadata and ticket ledger also contradict the
released implementation.

## Decisions

- Keep one `request()` signature and reject residual `**kwargs` explicitly so
  errors use this library's `ConfigurationError` contract.
- Add a typed `SFTPAuth`; preserve legacy `.login`/`.password` callers and the
  existing `protocol_info['client_keys']` route.
- Disable asyncssh agent/default-key discovery unless explicitly opted in.
- Warn for ordinary unknown protocol options in 1.x; reject security-sensitive
  unknown options immediately; make all unknown options errors in 2.0.
- Allocate AGW-43 because historical commits already use AGW-39 through
  AGW-42 while the stale index still claimed 39 was next.

## Acceptance criteria

The acceptance criteria are AC1–AC4 in
`docs/specs/p0_contract_corrections_spec.md`. This ticket is DONE only when
every criterion is covered by tests, the full gate is green, the changelog and
migration docs are current, and the implementing commits are recorded here and
in `index.json`.

## Work Log

- 2026-08-20 — Audited the current default branch, CI, package metadata,
  protocol registry, processor dispatch flow, SFTP connect options, release
  state, and ticket store. Confirmed 3,129 tests pass with 100% line/branch
  coverage on Python 3.14; wheel and sdist build, check, install, and import.
  Found the three P0 behaviors and stale/colliding ticket allocations. No code
  changed during the audit. Files: repository-wide read-only review.
- 2026-08-20 — Wrote the compatibility contract, edge cases, implementation
  map, and dispatch-control ADR before implementation. Files:
  `docs/specs/p0_contract_corrections_spec.md`,
  `docs/decisions/0002-processors-do-not-control-dispatch.md`, this ticket,
  and project wiki indexes.
- 2026-08-20 — Implemented the contract in
  [`4888ea6`](https://github.com/ajyadav013/asyncio-gateway/commit/4888ea6):
  immutable pre-dispatch URL/protocol fields, strict top-level keywords,
  per-protocol option censuses, typed SFTP authentication, fail-closed ambient
  identity handling, corrected package metadata, tests, README, changelog,
  security policy, specification, and ADR.
- 2026-08-20 — Verified Python 3.14 with 3,161 passed / 8 skipped at 100% line
  and branch coverage under all five committed shuffle seeds; Python 3.10 with
  3,152 passed / 17 skipped at 100%; flake8 source and format gates,
  suppression checker, mypy, Bandit, and example compilation all passed. The
  wheel and sdist built, passed `twine check`, installed into clean external
  environments, and imported the documented public API. Docker integration
  passed all 95 live-protocol tests. Reconciled the full ticket ledger while
  preserving AGW-32's unmet annotated-tag obligation and three historical ID
  collisions.
