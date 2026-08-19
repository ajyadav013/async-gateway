# AGW-37: Blocking TLS context construction reached from `_tls_value` (`tls_context_for`)

- **Status:** OPEN
- **Severity:** Critical (auto-Critical: blocking I/O on an async path —
  `.claude/rules/quality-gates.md` §1)
- **Story:** **none — owner unassigned.** See *Ownership* below; this needs an owner before the
  acceptance gate.
- **Spec:** `docs/specs/v1_release_spec.md` — R20 (no blocking I/O on an async path)
- **Files (declared scope):** ~`async_gateway/logic/ftp_client.py` (`tls_context_for`, line 145)

## Why

`def tls_context_for(...)` is a **module-level plain function** (`ftp_client.py:98`) whose line 145
is:

```python
return ssl.create_default_context()
```

It is called at `:371` from `async def _tls_value` (`:320`), which is awaited at `:291`. So it
performs the same blocking read of the system CA bundle described in AGW-36 (**194 certificates,
~6-15 ms** depending on page cache, measured) directly on the event loop.

Two properties make this worse than it looks:

1. **It is the default path, not an edge case.** `:145` is the **no-certificate** branch, so it
   fires on *every* verifying FTPS session — not only on mTLS-configured ones.
2. **It is structurally invisible to the R20 guard.** The AST scan added in S14
   (`tests/test_no_blocking_io.py`) searches `async def`s and plain `def`s *nested inside* them. It
   deliberately does **not** search module-level plain `def`s, because defining a blocking helper at
   module level is the intended escape hatch for the legitimate
   `run_in_executor` case. `tls_context_for` uses that hatch **without** an executor: it is called
   directly from a coroutine.

This is why it is **not** in the scan's `NOT_YET_REWRITTEN` allowance — the scan never produces an
offence for it, so an allowance entry would suppress nothing and would falsely imply the site is
mechanically tracked. It is documented in the scan module's *Known limitations* instead.

An independent two-pass AST sweep of all 24 package modules during the S14 review confirmed
`tls_context_for` is the **only** live instance of this shape in the package.

## Ownership — why this is unassigned

Deliberately left open rather than forced into a convenient story:

- **S17** is the TLS story, but its boundary is `helpers/internal/filters_helper.py` +
  `tests/helpers/test_filters_helper.py` + `tests/fixtures/tls.py`. It does **not** own
  `logic/ftp_client.py`.
- **S18** (γ lane of W13) *does* own `logic/ftp_client.py`, but runs **concurrently** with S17 in
  W13. Extending S17 into `ftp_client.py` would break W13's pairwise-disjoint boundary guarantee
  (β ∩ γ would stop being ∅), which is the property that makes that wave safe to parallelise.
- **S19** (spec Step 17) owns `logic/{http,ftp,sftp}_client.py` and runs after W13, so it is
  boundary-compatible — but its subject is verb allowlists and the URL-trust contract, not TLS.

**Orchestrator recommendation:** land it as a **scoped defect-loop commit after W13 and before or
alongside S19**, sharing whatever fix AGW-36 adopts in S17 (most likely a built-once, reused
context). Deciding it at that point costs nothing now and avoids both the W13 concurrency violation
and polluting S19's subject.

## Definition of Done

- `tls_context_for` no longer performs a blocking CA-bundle read on the async path — ideally by
  reusing the same mechanism AGW-36's fix establishes, so the two do not diverge.
- The no-certificate branch (`:145`) is covered, not just the certificate branch.
- The *Known limitations* paragraph in `tests/test_no_blocking_io.py` naming this site is updated or
  removed once the site is fixed.
- Consider whether the scan should grow a call-graph pass (module-level plain `def` reached from a
  coroutine). **Not required** — it is real new machinery and was explicitly out of S14's scope —
  but if this class recurs a third time, that is the durable fix rather than more prose.

## Dependencies

- **blockedBy:** nothing (but coordinate with AGW-36 so both adopt one mechanism)
- **blocks:** the acceptance gate should not pass with an unowned Critical

## Work Log

- **2026-08-17 (S14, iteration 4):** found by the `developer` while fixing AGW-36's containment,
  and flagged as out-of-boundary rather than silently fixed — the correct escalation. Verified
  independently by the orchestrator (lines 98/145/291/320/371) and by the reviewer's package-wide
  sweep, which confirmed it is the only instance of the shape.
