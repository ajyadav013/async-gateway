# AGW-36: Blocking TLS context construction on an async path (`get_ssl_config`)

- **Status:** DONE
- **Severity:** Critical (auto-Critical: blocking I/O on an async path —
  `.claude/rules/quality-gates.md` §1)
- **Story:** **S17** (spec Step 15, R23 — "A client TLS context that is actually a client TLS
  context"), which rewrites `get_ssl_config` and therefore owns both call sites.
- **Spec:** `docs/specs/v1_release_spec.md` — R20 (no blocking I/O on an async path) and R23 (TLS)
- **Decisions:** Ruling W (S14) — the *code* fix was deliberately kept out of S14's boundary; S14
  shipped a containment allowance instead. See below.
- **Files (declared scope):** ~`async_gateway/helpers/internal/filters_helper.py`
  (`get_ssl_config`, lines 58-59) — already inside S17's declared boundary.

## Why

Inside `async def get_ssl_config`:

```python
ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)   # :58
ssl_context.load_cert_chain(certificate[0], certificate[1])          # :59
```

Both are blocking filesystem reads on an async path. Measured on this machine during the S14
review:

- `create_default_context(SERVER_AUTH)` takes **14.60 ms** and loads **194 CA certificates**
  (`cert_store_stats: {'x509': 194, 'x509_ca': 194}`) from `/opt/homebrew/etc/openssl@3/cert.pem`.
- `load_cert_chain` opens two further files (proved by `FileNotFoundError [Errno 2]` on a missing
  path).

**Blast radius spans both transports**, and the two halves differ:

- **HTTP:** reached from `make_http_request` (`request_helper.py:215`) — i.e. **inside**
  `failsafe.run`, so it runs on *every attempt* of *every* certificate-configured request.
- **FTP:** reached at `ftp_client.py:353` from `async def _tls_value` (`:320`), awaited at `:291` —
  **outside** `failsafe.run` (FTP's failsafe use is `:391`/`:397` only).

S17 must cover both.

## Why S14 did not fix it

`filters_helper.py` is outside S14's four-file boundary and squarely inside S17's, whose entire
subject is rewriting this function. Forcing the fix into S14 would have been the boundary violation
this run has been disciplined about. Instead, **Ruling W** required S14 to stop *misrepresenting*
the situation:

- `'ssl.'` was added to the AST scan's `BLOCKING_MODULE_PREFIXES`, so the `:58` form is now a
  detected shape;
- a single containment allowance, `NOT_YET_REWRITTEN`, keyed on the **triple**
  `('helpers/internal/filters_helper.py', 'get_ssl_config', 'ssl.create_default_context')`, excuses
  exactly that one call spelling in that one function — any *other* banned call in the same
  function is still reported (mutation-proved by both the reviewer and the orchestrator);
- `test_the_allowance_holds_exactly_one_entry` pins the allowance at one entry, so it cannot grow
  quietly;
- the false prose claiming the package held the property was deleted.

`:59` (`load_cert_chain` on a bare local variable) is **structurally invisible** to a lexical AST
scan — it cannot be allowance-tracked, only documented, and it is named in the scan module's
*Known limitations*.

## Definition of Done (for S17)

- `get_ssl_config` no longer performs blocking filesystem work on the async path — either the
  context is built once and reused, or the build is moved off the loop; state which and why.
- Both call sites are covered: the HTTP path (inside `failsafe.run`) and the FTP path
  (`ftp_client.py:353`, outside it).
- **Delete the `NOT_YET_REWRITTEN` entry** in `tests/test_no_blocking_io.py`, and delete the
  `'get_ssl_config'` limitation prose naming `:59`. `test_the_allowance_holds_exactly_one_entry`
  will fail until the allowance is emptied or re-pointed — that failure is the reminder, and it is
  intentional.
- The AST scan passes with the allowance **removed**, not merely with it in place.
- A test proves the CA bundle is not re-read per request (e.g. count the reads across two requests).

## Dependencies

- **blockedBy:** nothing
- **blocks:** S17's own completion; the `security-clear` gate should not close with a known
  auto-Critical open.

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`767a872`](https://github.com/ajyadav013/asyncio-gateway/commit/767a872), [`9791abc`](https://github.com/ajyadav013/asyncio-gateway/commit/9791abc); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


- **2026-08-17 (S14 review, iteration 2):** found by the `sdlc-code-reviewer` while auditing S14's
  package-wide no-blocking-I/O claim; the scan reported the package clean while this call sat on the
  loop. Measured (194 certs / 14.60 ms). Routed to S17 under orchestrator Ruling W; S14 shipped the
  containment allowance and honest prose in place of a fix.
- **2026-08-17 (S14 review, iteration 3):** allowance found to be keyed `(module, function)`, which
  muted the *whole function*; re-keyed to the triple and re-proved.
