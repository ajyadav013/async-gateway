# AGW-34: `redact_url` returns unparseable URLs verbatim (S8-L1)

- **Status:** DONE
- **Story:** none — a residual finding from S8's review, held open for adjudication at the
  **security gate** (pipeline stage 5.4, `security-clear`).
- **Spec:** `docs/specs/v1_release_spec.md` — E9 (the redaction invariant) and its documented bound
- **Design:** `docs/specs/v1_release_spec.md` — Part B, the redaction contract owned by S7
- **Decisions:** Ruling L (S8) deferred the seam; the seam itself was then closed by the S8-H1 fix
  (`d359640`). This ticket is what remains after that fix.
- **Files (declared scope):** ~`async_gateway/utils/redaction.py` (`redact_url`) — **no story owns
  it**; the security gate assigns one if the finding is upheld.

## Why

`redact_url` masks sensitive query parameters by parsing the URL. When the parse fails, it returns
the input **unchanged** rather than falling back to a conservative mask:

```
redact_url('http://[bad/p?api_key=S')  ->  'http://[bad/p?api_key=S'   (measured, S8 review)
```

`http://[bad/...` is an unterminated IPv6 literal, so `urlsplit` raises and the value is echoed with
`api_key=S` in the clear.

**Severity: Low, and deliberately so.** Three things bound it:

1. It is **inside S7's documented E9 bound** — E9 promises redaction of *parseable* URLs, and this
   case is named there rather than discovered here.
2. The caller-visible surfaces are already covered by a second, independent mechanism. The S8-H1
   fix (`d359640`) made `redact_text` mask sensitive `name=value` pairs after `?` or `&` **with no
   URL-ness predicate at all** — matched both as written and percent-decoded. Every consumer that
   composes the two (`redact_value` -> `redact_text` then `redact_url`) is therefore already
   protected on this input; it is `redact_url` *called directly* that echoes.
3. Reaching it requires the caller to supply a syntactically invalid URL, which the entry point's
   own validation (S8) rejects before dispatch on the normal path.

It is nonetheless a redactor that fails **open**, and this project has now had a URL-classification
predicate fail open three times (S7 channel 4, S8's `_is_absolute_url`, S8-H1). That pattern is the
reason this is tracked to a gate rather than closed as won't-fix.

## What the security gate must decide

- **Uphold and fix:** on a parse failure, fall back to masking rather than echoing — e.g. route the
  value through `redact_text` (which needs no parse) and return that, so the function's failure mode
  becomes over-masking instead of leaking. This is the option the orchestrator would recommend: it
  reuses the seam that already exists, adds no new predicate, and makes the fallback direction
  consistent with every other redactor in the module.
- **Accept and document:** record it as a named bound of E9 in the README's redaction section, with
  the composition note that `redact_value` covers it. Acceptable **only** if the gate confirms no
  caller-visible surface reaches `redact_url` uncomposed.

Explicitly **not** on the table: tightening the URL parse to "recognise more shapes". That is the
predicate that has failed open three times; the learning of record (`.claude/agent-memory/`) is that
when a classifier fails open twice you delete it, not tighten it.

## Definition of Done

- the security gate returns a verdict (uphold+fix, or accept+document) with its reasoning recorded
- if upheld: the fix lands in the story the gate assigns, with a test proving
  `redact_url('http://[bad/p?api_key=SECRET')` no longer contains `SECRET`, plus the idempotence and
  non-sensitive-untouched checks the module's other redactors already carry
- if accepted: the bound is stated in the README (S29 owns that file) and in `redact_url`'s docstring

## Dependencies

- **blockedBy:** nothing
- **blocks:** the `security-clear` gate cannot close with this unadjudicated

## Decisions

- **S8 / Ruling L:** not fixed in S8 — widening the URL pattern risked over-masking prose and
  altered the contract surface S7 owns.
- **Post-S8-H1 re-assessment (this invocation):** the seam fix closed the caller-visible surfaces,
  which is why this stays Low rather than being escalated.

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`0e223d2`](https://github.com/ajyadav013/asyncio-gateway/commit/0e223d2), [`82b6e44`](https://github.com/ajyadav013/asyncio-gateway/commit/82b6e44), [`a061629`](https://github.com/ajyadav013/asyncio-gateway/commit/a061629); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


_Empty — opened when the finding was routed to the security gate._
