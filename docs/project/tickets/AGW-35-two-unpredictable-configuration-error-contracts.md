# AGW-35: two unpredictable `ConfigurationError` contracts across protocols

- **Status:** OPEN
- **Severity: Medium.** Blocks the **acceptance gate**. Does not block any code-review gate — the
  two stories that exposed it are each internally correct and were each approved on their own
  boundary.
- **Story:** none — a cross-cutting inconsistency surfaced by holding S12 and S13 in one tree.
  **Owner: the entry point (`async_gateway/async_gateway.py`) / the S12 lane.**
- **Spec:** `docs/specs/v1_release_spec.md` — R11 ("validate once, at the boundary") and the
  Ruling G record. **The spec does not currently say which mechanism applies where; that is the
  gap.**
- **Files:** `async_gateway/async_gateway.py:298-311` (the docstring making the universal claim) ·
  `async_gateway/logic/sftp_client.py:81-83, 520-522, 534` (the counter-example and its
  self-contradicting docstring)
- **Discovered:** S13 code review, iteration 2, by the `sdlc-code-reviewer`. **Independently
  re-measured by the orchestrator before being accepted** — see below.

## Why

Two configuration mistakes of the same category, both detectable before a byte leaves the process,
produce opposite outcomes:

| Call | Outcome |
|---|---|
| HTTP, `http_file_upload_config` + `request_type='get'` | **raises** `ConfigurationError`, **not** logged |
| SFTP, `protocol_info={}` (no `mode`) | **`ok=False` envelope**, `code='CONFIG'`, **and** logged ERROR |

Orchestrator's own measurement at `6391430`, through the public `request()`:

```
SFTP no-mode -> NO RAISE | ok= False | code= CONFIG
logged: [('async_gateway.async_gateway', 'ERROR')]
```

A caller must therefore write **both** a `try/except ConfigurationError` **and** a
`result['error']['code'] == 'CONFIG'` check, with no documented rule for which protocol does which
short of reading each client's source.

It is worse than an internal inconsistency, because the public docstring documents only one of the
two **as universal**. `async_gateway.py:298-311` says configuration errors "escape synchronously
rather than becoming an envelope a retry loop would re-attempt forever — and, escaping, they are
reported to the caller exactly once **and are not also logged**. **Every one of them** is raised
before anything is dispatched." The SFTP `mode` error satisfies none of the three clauses.

## The orchestrator ruling this overturns — recorded, not quietly dropped

Ruling T's consistency note asserted that the governing line is **pre-dispatch vs post-dispatch**,
not constructor vs method, and that both placements were therefore correct. **That rationale is
wrong, and the reviewer refuted it with measurement.** `_validate_mode()` is the first statement of
`handle_request` (`sftp_client.py:534`) and runs before `_run_session()` — by any definition a
caller would use, *nothing has been dispatched*. Yet it yields an envelope.

The line that actually decides is mechanical and internal: **is the raise outside or inside the
`try` at `async_gateway.py:376`** — i.e. constructor vs method after all. The distinction the ruling
drew does not exist in the code.

The cost of that error is visible in the tree: S12's module docstring now contradicts itself,
because the orchestrator instructed the developer to document the wrong framing. `:81-83` asserts
"the line between them is dispatch, not method"; `:520-522` concedes "because it is raised here
rather than in `__init__`". Both sentences shipped in `b6e58ba`. Fixing that docstring is part of
this ticket.

**The placements themselves both stay.** S13's is the conforming one. S12's is forced: R11-AC3
(`tests/test_entrypoint.py:206-211`) requires `SFTPRequest` to be constructible with
`protocol_info=None`, so SFTP genuinely cannot validate `mode` in `__init__`. The defect is the
*undocumented divergence*, not either choice.

## Recommended resolution (orchestrator's recommendation, for the acceptance gate)

**Make the entry-point docstring tell the truth**, rather than trying to unify the behaviours.
Unification is not available: R11-AC3 blocks the constructor route for SFTP, and moving HTTP's
checks *into* `handle_request` would make them envelopes, which is the weaker contract and would
undo S13's approved fix.

So state the rule plainly and make it predictable:
- Configuration errors detectable from the caller's arguments **alone** are raised synchronously
  before dispatch and are not logged.
- Configuration errors that a protocol cannot detect until `handle_request` — because an approved
  criterion requires the request object to be constructible without that argument — are returned as
  an `ok=False` / `CONFIG` envelope and are logged once.
- Name which protocols are in the second set and why, so a caller reads one paragraph rather than
  three clients' source.

The alternative — amend R11-AC3 so SFTP may declare `mode` in `REQUIRED_INFO_KEYS`, unifying on
synchronous escape — is cleaner in the end state but reopens an approved, tested criterion from an
earlier story. Route that decision to the acceptance gate rather than settling it here.

## Definition of Done

- `async_gateway.py:298-311` states the real rule, including which protocols raise and which
  envelope, and no longer claims "every one of them" is raised pre-dispatch
- its `:raises ConfigurationError:` enumeration is extended with S13's GET + `http_file_upload_config`
  rejection (recorded separately as the Low below)
- `sftp_client.py:81-83` and `:520-522` no longer contradict each other
- a test asserts *both* contracts through the public `request()`, so the divergence is pinned rather
  than rediscovered
- the README's error-handling section documents both paths (S29 owns the README)

## Related, same owner

**Low — `async_gateway.py:298-311`'s `:raises:` list was not extended by S13.** The list is
introduced by "That is:" and reads as exhaustive; it stops at the non-callable `serialization` case
and never mentions the new GET + upload rejection, which is in exactly that class. Closes naturally
with the main fix.

## Dependencies

- **blockedBy:** nothing
- **blocks:** the `acceptance` gate

## Work Log

_Opened at the close of wave W10, when S12 and S13 first sat in one tree._
