# AGW-8: Dispatch, validation, the one exception→envelope boundary

- **Status:** DONE
- **Story:** S8 — spec Step 6, size M (`docs/specs/v1_release_stories.md` §4, Phase 1)
- **Spec:** `docs/specs/v1_release_spec.md` — R11-AC1,2,3,4,6,7 (Group D — Entry point and protocol dispatch) · R10-AC3 (Group C — The public response contract and the error model)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; Architecture overview, `request()` and the protocol registry
- **Decisions:** none
- **Files (declared scope):** ~`async_gateway.py`, `logic/__init__.py`, `helpers/internal/base.py` · +`tests/test_entrypoint.py`

## Why

R11 requires dispatch that validates its input and cannot crash on the documented call: the protocol
string is normalised once and the same value serves both the guard and the registry lookup, and
`protocol_info` is checked rather than read raw. R10-AC3 fixes the single exception→envelope boundary
so that a library error becomes a populated envelope while a programming error still escapes. FI-14
puts the scheme check *after* the pre-processors, against the URL actually dispatched — the property
whose absence concealed C6, H6 and most of the audit.

Closes findings: H4, H5, H6, L2. Discharges FI-14.

## Definition of Done

- protocol normalised **once**, same value for guard and lookup (parametrised: lower/mixed/whitespace × 5 protocols)
- `None`/`''`/`123`/unknown → `ConfigurationError` naming supported protocols
- `protocol_info` `None` and `{}` work where nothing is required, else `ConfigurationError` naming the key; `base.py:31-37` no longer reads the **raw** `info` after guarding it
- **FI-14:** `'HTTPS'`+`http://` → error, schemeless → upgraded, `'HTTP'` accepts either — checked against the URL **actually dispatched**, after pre-processors
- registry typed `dict[str, type[BaseRequestClass]]`
- **an injected `KeyError`/`TypeError`/`UnboundLocalError` escapes `request()`** rather than becoming an envelope (the property whose absence concealed C6, H6 and most of the audit)

## Dependencies

- **blockedBy:** AGW-7
- **blocks:** AGW-9

## Decisions

_None recorded yet._

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`a16a324`](https://github.com/ajyadav013/asyncio-gateway/commit/a16a324), [`d359640`](https://github.com/ajyadav013/asyncio-gateway/commit/d359640); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


### Implementation (developer, S8)

**Changed**

- `async_gateway/logic/__init__.py` — registry retyped
  `Final[dict[str, type[BaseRequestClass]]]`; the `'SOAP'` entry removed entirely
  (Ruling J), so SOAP is an unknown protocol until AGW-22 registers its class.
- `async_gateway/helpers/internal/base.py` — new `validated_protocol_info()`, the
  one implementation of "is this `protocol_info` usable"; new class attribute
  `REQUIRED_INFO_KEYS` (default `frozenset()`), so the required-key set is
  declared by each protocol and this file names none of them. Every post-guard
  read now goes through `self.info` (H5).
- `async_gateway/logic/http_client.py` — `HttpRequest.REQUIRED_INFO_KEYS =
  frozenset({'request_type'})`. `self.info['request_type']` already read the
  guarded mapping, so the line itself is unchanged; it can no longer `KeyError`.
- `async_gateway/async_gateway.py` — new `resolve_protocol()` (normalise once,
  one value for guard and lookup — H4) and `dispatch_url_for()` (scheme
  enforcement for the HTTP family only — H6, FI-14). Both run at the boundary:
  the protocol and `protocol_info` before the envelope exists, the scheme check
  after the pre-processor and against the same variable that is then handed to
  the protocol object. The envelope's `protocol` and the failure log now carry
  the normalised name and the dispatched URL.
- `tests/test_entrypoint.py` — new, 78 tests.

**Decisions**

- Pre-dispatch validation raises and does **not** log (Ruling OQ14, single-report
  principle), asserted by
  `test_r11_ac2_a_rejected_protocol_is_reported_once_and_not_logged`.
- `host:8080/p` (no scheme, host parses *as* a scheme) is refused rather than
  guessed at. Recorded as a test row, not left implicit.

**Verified** — `pytest` (see the conflict below), `mypy async_gateway tests`
clean (36 files), `flake8 async_gateway tests` 39 findings, byte-identical to the
39 at `af16cf6` (no new ones), `grep -rn "'SOAP': None" async_gateway/` → 0.
R11-AC6 proven mypy-visible: assigning `None` into the registry is
`error: Incompatible types in assignment ... target has type
"type[BaseRequestClass]"`.

**Blocked — out-of-boundary conflict.** R11's edge case makes `ftpx://` under
`protocol='HTTP'` a pre-dispatch `ConfigurationError`, which is exactly what
`tests/test_envelope.py:1240` (`NON_HTTP_URL`) relied on reaching `aiohttp` for.
Two S7 tests therefore fail. That file is outside this story's declared scope;
reported to the orchestrator rather than edited.

### Resolution — Ruling K (scope extension)

**Unblocked.** Ruling K, human-ratified, extends S8's file boundary to
`async_gateway/utils/redaction.py` and `tests/test_envelope.py`.

**DEVIATION from one-story-one-boundary.** An S7 seam fix lands in an S8 commit.
Rationale: the S7 escape is reachable only through S8's new scheme enforcement, so
the two cannot be separated in time — splitting them would mean committing a
knowingly-red suite.

**Security substance.** `redact_value()` masked a string only when it parsed as an
absolute URL *with a netloc*. `http:///p?api_key=SECRET` has a scheme and no
netloc, so it failed that gate and was echoed verbatim into log records through
`log_failure`'s extra values, while the envelope itself was correctly masked — a
Critical secret-in-logs leak, pre-existing from S7 and merely exposed here. The
whole-string `_is_absolute_url` gate could not see it: it asked "is this entire
string a URL I recognise?" and answered no, which discarded the string wholesale
instead of scanning it for secrets. The fix routes every `str` through
`redact_text` (netloc-agnostic, a no-op on strings containing no URL) and deletes
`_is_absolute_url`, so the seam now fails **closed** — unrecognised input is
redacted, not passed through. Fixed at the seam, not at the call sites; third
recurrence of that lesson in this run.

**Verified** — `pytest` 261 passed / 1 xfailed; coverage 76.41%, `fail_under`
ratcheted 72.90 → 76.41 (R28, up only); `mypy` clean (36 files); `flake8` zero new
findings vs `af16cf6`. `tests/test_envelope.py`'s vehicle was repointed from
`ftpx://` (`NON_HTTP_URL`) to `http:///p?...` (`NO_NETLOC_URL`), asserting
`InvalidUrlClientError` — confirmed by running aiohttp, not assumed. Guard test
`test_redact_value_masks_a_url_that_has_no_netloc` added and mutation-proved:
restoring the old gate fails 3 tests.

**Left open (Low).** `redact_url` still returns an unparseable URL (e.g.
`http://[bad/...`) verbatim. That sits inside S7's documented E9 bound; to be
adjudicated at the security gate. S7 is **not** reopened for it.

### Resolution — Ruling L (scope boundary held)

**The Ruling K fix was itself incomplete, twice over.** `redact_value` is the
single entry point for the scalar values in the failure log's `extra`, and it
guarded masking behind a predicate that tried to classify "is this a URL?". That
predicate failed open twice: first it required a scheme **and** a netloc, so
`http:///p?api_key=S` leaked (the original Critical); the Ruling K fix replaced
it with a `redact_text` pass plus a second `redact_url` pass gated on a truthy
scheme, which still failed open on a **scheme-less** URL (`host/p?api_key=S`).
The lesson, stated plainly: **the predicate was the defect, not any particular
version of it.** The fix removes it — `redact_value` now unconditionally composes
`redact_url(redact_text(value))`, masking whatever the envelope masks by
construction rather than by a classification test that keeps being wrong.
Text-pass-first ordering is load-bearing and is documented in the function.

An intermediate review round fixed, before this: a whole-string URL whose secret
value contained a space/quote/backtick/angle bracket leaked its tail;
`validated_protocol_info` ran after the caller's pre-processor and ran twice;
`_EMBEDDED_URL` was quadratic on long strings with no `://` (200 KB took 35.0 s,
blocking the event loop — now bounded, 0.015 s); and the envelope reported a
different URL than was dispatched.

**Escalated, deliberately NOT fixed here (open High, needs a human decision).**
`_EMBEDDED_URL` requires `scheme://`, so `redact_text` is a total no-op on a
scheme-less URL. A scheme-less URL carrying a caller-declared secret therefore
still leaks on two further surfaces: the log's `extra['traceback']`, and —
caller-visible — the returned envelope's `error['message']` and `error['cause']`
(via `exceptions.py:257-264`). Only the `extra['url']` surface is closed by this
story. Widening `redact_text`'s pattern to match scheme-less strings risks
over-masking arbitrary prose and changes the contract surface story S7 owns, so
it is a human decision, not an orchestrator one. The implementing developer
**stopped and refused** an earlier instruction resting on the false premise that
this was one surface — that escalation is why the scope here is correct.

**Coverage ratchet.** `fail_under` 72.90 → **76.44** against the committed
baseline at `af16cf6` (+3.54; the 76.69 seen mid-story was never committed). It
fell from 76.69 to 76.44 because the final fix deleted 7 covered statements and
2 covered branches — missed statements stayed at **159**, so no line lost
coverage; the denominator shrank. Also added `precision = 2`, because
`coverage.py` compares `fail_under` against the total **rounded to `precision`**
(default 0) — the project's two-decimal ratchet had been silently
integer-precise until now.

**Verified** (measured by the orchestrator, not self-reported) — `pytest` exit 0,
**274 passed / 1 xfailed / 0 failed**, coverage **76.44%**; `mypy` clean, 36
source files; `flake8` 39 findings, zero new versus `af16cf6`. Mutation-proved:
replacing the composition with `redact_text` alone fails 8 tests, the mutation
confirmed landed by md5 before the run counted.
