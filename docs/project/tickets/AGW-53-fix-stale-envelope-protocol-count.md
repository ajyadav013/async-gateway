# AGW-53: Fix stale envelope protocol count

- **Status:** OPEN
- **Branch:** `codex/gcs-selector-backend`
- **Story:** Stage 5 discovered Low-severity fast-track defect
- **Spec:** [GCS selector specification](../../specs/gcs_selector_spec.md)
- **Evidence:** [Contract Clear gate](../../../.Codex/artifacts/gcs-selector/contract-clear-gate.md), [full tester report](../../../.Codex/artifacts/gcs-selector/full-tester-report.md), and [senior tester report](../../../.Codex/artifacts/gcs-selector/full-senior-tester-report.md)
- **Decisions:** No new architecture decision; the registered-selector contract remains the source of truth.
- **Files (declared scope):** `asyncio_gateway/utils/envelope.py`, `tests/test_docs.py`

## Why

Contract Clear and the independent Stage 5 full and senior tester reviews found
the same Low-severity documentation drift: `asyncio_gateway/utils/envelope.py`
says the shared envelope spans nine protocols while the selector registry has
ten. This fast-track defect keeps the correction behavior-neutral and adds an
anti-drift check; the finding remains open until implementation and review.

## RED-first plan and definition of done

- **RED first:** Add a focused anti-drift assertion in `tests/test_docs.py`
  that derives the registered-selector contract and fails when any numeric
  protocol-count claim in the envelope module docstring disagrees with it.
  Prove the current nine-versus-ten wording fails before editing the source.
- **GREEN/refactor:** Change only the module docstring in
  `asyncio_gateway/utils/envelope.py`. Remove the stale hardcoded count, or
  keep a count only when it is accurately bound to the registered-selector
  contract. Do not alter runtime behavior.
- **Done checks:** The source diff is docstring-only; normalized executable AST
  is unchanged; the focused selector and full `tests/test_docs.py` pass; full
  repository coverage and lint remain green; no live GCP action occurs.

## Work Log

- 2026-08-23 — Opened the Stage 5 Low-severity defect before implementation;
  recorded the two-file implementation boundary, gate/test evidence, RED-first
  proof, and behavior-neutral completion checks. Files: this ticket and the
  local ticket index. The finding remains open.
