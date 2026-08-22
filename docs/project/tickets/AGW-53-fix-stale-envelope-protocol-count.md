# AGW-53: Fix stale envelope protocol count

- **Status:** IN PROGRESS
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
- 2026-08-23 — RED began from a 243/243 focused docs-test baseline. One
  anti-drift test now derives the current registry size for diagnostics,
  requires the count-independent affirmative wording ``all registered
  protocols``, and rejects numeric protocol-count claims. The focused suite
  collects 244 tests with 243 passing and exactly this new test failing on the
  existing ``all nine protocols`` wording. Test SHA-256 is
  ``c91a55b1278007a5954901a8db3f8a5eebe3980a7f81cb759ea0face21b0fea9``;
  ``asyncio_gateway/utils/envelope.py`` remains unchanged at SHA-256
  ``930f6764eb4f902370ed33150cab3b1afbc1f61d2c7343d791e6f8b1e4afe7f1``.
  Production GREEN, commit, and delivery actions remain pending.
- 2026-08-23 — Independent oracle review found that the first numeric matcher
  would also reject ordinary singular prose such as ``one protocol used to
  build a fresh dict``. The matcher is now narrowed to count claims introduced
  by ``all`` or ``across`` (including ``across all``), with optional
  ``registered`` before ``protocol(s)``. A five-row simulation rejects ``all
  nine protocols``, ``across all nine protocols``, and ``across 10 registered
  protocols`` while accepting the ordinary singular phrase and the exact
  count-independent target phrase. Scoped flake8 and byte-compilation pass;
  the focused no-coverage suite remains genuine RED at 244 collected, 243
  passed, and exactly one failure on the stale envelope wording. Corrected
  test SHA-256 is
  ``8b494848cddc66abf9dc41fe545d7dc22d5663d4b55acff0fed94769b30e2851``;
  ``asyncio_gateway/utils/envelope.py`` remains unchanged at SHA-256
  ``930f6764eb4f902370ed33150cab3b1afbc1f61d2c7343d791e6f8b1e4afe7f1``.
  Status remains IN PROGRESS pending production GREEN and independent review.
- 2026-08-23 — GREEN replaced only ``all nine protocols`` with ``all
  registered protocols`` in the envelope module docstring. The targeted
  anti-drift node passes 1/1, the focused docs suite passes 244/244, and the
  full repository passes 5,928 tests with 20 declared skips and 100% statement
  and branch coverage. Stripping the module docstring leaves the normalized AST
  identical to HEAD; flake8, the exact CI format and suppression checks, mypy,
  byte-compilation, JSON validation, and diff/scope checks pass. The accepted
  test remains byte-identical at SHA-256
  ``8b494848cddc66abf9dc41fe545d7dc22d5663d4b55acff0fed94769b30e2851``.
  Status remains IN PROGRESS pending independent review.
