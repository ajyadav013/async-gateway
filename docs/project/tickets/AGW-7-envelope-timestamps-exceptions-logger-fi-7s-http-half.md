# AGW-7: Envelope · timestamps · exceptions · logger · FI-7's HTTP half

- **Status:** DONE
- **Story:** S7 — spec Step 5, size **XL** — 13 files, cannot be split (`docs/specs/v1_release_stories.md` §4, Phase 1; sizing exception §12)
- **Spec:** `docs/specs/v1_release_spec.md` — R8 (all but AC2, AC12), R9 all, R10 (all but AC3) (Group C — The public response contract and the error model) · R6-AC2 (Group B — Dependency upgrades and the resilience-library decision) · Part B invariants **E1–E11**
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; Architecture overview, the envelope contract
- **Decisions:** none
- **Files (declared scope):** +`utils/{envelope,status_map,redaction}.py` · rewrite `utils/exceptions.py` · ~`helpers/common/date_helper.py`, `utils/constants.py`, `helpers/internal/base.py`, `__init__.py`, `async_gateway.py`, `helpers/internal/request_helper.py`, `logic/http_client.py`, `pyproject.toml` · +`tests/{test_envelope,test_exceptions}.py`

## Why

R8, R9 and R10 are *the contract*: one response envelope returned by every protocol with a success
predicate that can fail, timezone-honest timestamps with monotonic latency, and a real exception
hierarchy whose errors are never empty. R6-AC2 removes `pytz` in the same commit as the last IANA
lookup, because the import and the declaration must move together or the clean-venv job breaks in one
direction. Part B's invariants E1–E11 land here as tests, which is what makes every later protocol
story checkable against a fixed shape. Three constraints forbid splitting this step (§12): FI-3 needs
the exception hierarchy and the envelope in one commit, FI-7's defect is *at the seam* so its fix must
be too, and the `pytz` atomicity rule binds the last two files together.

Closes findings: C2, H7, H27, H29, M20, MG3, L3, L4, L11, L12. Discharges FI-3 and FI-7 (HTTP half).

## Definition of Done

- `GatewayResponse` defined **once**; `new_envelope`/`finalise_ok`/`finalise_error` the only constructors; **E1–E11 exist as tests**
- `grep -rn "999" async_gateway/` → 0 (E7)
- `grep -rn "'tat'"` → 0
- `api_response` absent (test asserts `KeyError`)
- `grep -rn "except Exception" async_gateway/` → 0
- **FI-3:** `RetriesExhausted() from ClientConnectorError` yields a non-empty message + `cause` naming the connector; `unwrap_cause` depth-bounded, cyclic chain terminates *(a naive `str(exc)` yields `''`, which reads as success and is worse than 999)*
- **FI-7:** delete `request_helper.py:93`'s `kwargs.get('response', {})`; `make_http_request` returns typed `HttpResult`; `grep -rn "kwargs.get('response'"` → 0
- **E11:** a 404 with a JSON error body → `ok False`, `status_code 404`, non-empty `text` **and** populated `json`; `finalise_error` only adds `error` and flips `ok`
- UTC ISO-8601 `request_time`; `grep -rn "time.time()"` → 0; clock patched backwards still `latency >= 0`
- `grep -rn "Asia/Kolkata\|TIMEZONE\|pytz" async_gateway/` → 0 **and the `pytz` declaration drops in this same commit** (import and declaration must move together or the clean-venv job breaks in one direction)
- one redactor serving **both** envelope and logger — header, `?api_key=`, `{'password':…}` and `Set-Cookie` all absent from `repr(result)`; **no `redact=False` exists**
- exactly one handler on the `async_gateway` logger and it is a `NullHandler`; `CancelledError` propagates

## Dependencies

- **blockedBy:** AGW-6
- **blocks:** AGW-8

## Decisions

- **Ruling F (ratified before implementation) — boundary override for
  `utils/http_file_config.py`.** The spec removes `CustomGlobalException`
  outright (`docs/specs/v1_release_spec.md:3129`, "removed, not deprecated"),
  and that file was its only consumer: an import at `:11` and one raise at
  `:62`. Both are re-pointed at the new hierarchy's `HttpStatusError` with the
  message text preserved verbatim. Nothing else in the file is touched —
  S20/R25 rewrites the whole function later (H16).
- **Ruling G (ratified) — a caller-configuration error escapes `request()`
  synchronously.** Spec `:3815-3818` makes every caller-config failure raised
  *before dispatch* a "— (**raises**)" row: it does not become an envelope,
  because returning one would invite a retry loop against a call that can
  never succeed. Only a `ConfigurationError` raised *inside* `handle_request`
  becomes a 400 envelope. Now a named test rather than an accident:
  `tests/test_exceptions.py::test_a_configuration_error_escapes_request_synchronously`.
- **S5's `ConfigurationError` is absorbed, not duplicated or dropped.** It is
  reparented under `AsyncGatewayError` keeping its name, its one-argument
  call and its 400 mapping, so `async_gateway.py`'s `:raises
  ConfigurationError:` docstring stays true. Its second parameter changed
  meaning (`error_data: Dict` → `status_code: int`), so the two call sites in
  `logic/http_client.py` that passed a context dict positionally now pass
  nothing — the dict's content was already spelled out in the message. See
  the defect note below.
- **`redact_cookies` is a fifth redactor the spec's API list at `:3451` does
  not name.** Invariant E9 requires that no cookie value reach the envelope,
  and a cookie *name* carries no signal about whether its value is a
  credential (a session id is as sensitive as an `Authorization` header). So
  every cookie value is masked rather than a guessed subset.
- **Coverage floor truncated, not rounded.** Measured 71.936759%; the floor
  is 71.93. Rounding to 71.94 puts the floor above the value it was measured
  from and fails the very run that set it (verified — see the Work Log).

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`af16cf6`](https://github.com/ajyadav013/asyncio-gateway/commit/af16cf6); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


### 2026-08-16 — implementation (S7 / Step 5)

**What changed.** The contract every remaining story is written against:

- **`utils/envelope.py` (new)** — `GatewayResponse` and `GatewayError` are
  declared once here and nowhere else, with `new_envelope` / `finalise_ok` /
  `finalise_error` as the only constructors. `finalise_error` *adds* the error
  and flips `ok`; it never clears `status_code`, `headers`, `cookies`, `text`
  or `json`, which is invariant E11.
- **`utils/status_map.py` (new)** — one table from wire-stable error code to
  status code. The fabricated three-digit status is replaced by a real one on
  every path.
- **`utils/redaction.py` (new)** — one redactor serving both the envelope and
  the logger, so the two cannot disagree about what is a secret. Header and
  cookie values, URL userinfo and sensitive-named query parameters, and
  payload values masked by key name to depth 4. There is no `redact=False`.
- **`utils/exceptions.py` (rewritten)** — the hierarchy rooted at
  `AsyncGatewayError`, every class calling `super().__init__(message)` so
  `args` is populated and instances pickle, plus depth-bounded `unwrap_cause`.
- **`helpers/common/date_helper.py`** — rewritten on stdlib `datetime` UTC and
  `time.monotonic`. No third-party timezone library, no `zoneinfo`, no IANA
  name resolved at all (OQ11), and therefore no `tzdata` on any platform.
- **`pyproject.toml`** — the timezone dependency's declaration drops in this
  same change as its last import, per the atomicity rule.
- **`helpers/internal/request_helper.py`** — FI-7's HTTP half: the fresh
  response dict pulled from an optional keyword no caller passed is deleted,
  and `make_http_request` returns a typed `HttpResult`.
- **`logic/http_client.py`** — copies that `HttpResult` into the envelope it
  was handed *before* raising any status error (E11), and classifies what the
  resilience layer wraps into typed transport errors.
- **`async_gateway.py`** — the single conversion point, logging each failure
  once with redacted structured `extra`.
- **`__init__.py`** — a `NullHandler` on the `async_gateway` logger, and
  nothing else.

**Defects found in the pre-existing uncommitted draft and fixed.**

1. `ConfigurationError(msg, {...})` in `logic/http_client.py` passed a context
   dict into the new second parameter, which is `status_code`. The resulting
   envelope carried `status_code={'returned': 'bytes'}` — a non-integer status
   that also breaks E5's `json.dumps`. Both call sites corrected.
2. Three module docstrings quoted the very tokens the acceptance greps ban
   (`999` in `status_map.py`, the timezone library's name in `date_helper.py`,
   the deleted keyword expression in `request_helper.py`), so the greps
   matched on prose. Reworded.
3. `http_file_config.py` interpolated the remote response body into the error
   message, which is neither "verbatim" nor safe. Reduced to the original
   message text.
4. Latency was measured from two different origins — `BaseRequestClass.start_time`
   on success, a second clock read in `request()` on failure. Unified on the
   protocol object's own reference point.
5. `redact_url` percent-encoded the sentinel, so a masked parameter was
   indistinguishable from a real one. Kept literal via `urlencode(safe='*')`.
6. **`tests/test_envelope.py` did not exist** — invariants E1-E11 had never
   been written. Now 66 tests covering E1-E11, R9's clocks, R10's logger tree
   and the redactor.

**Verification** (commands run, real output):

- `pytest` → `158 passed, 1 xfailed`; `Required test coverage of 71.93%
  reached. Total coverage: 71.94%`.
- `mypy async_gateway` → `Success: no issues found in 24 source files`.
- `flake8` over the whole repo → 1275 findings, **down from 1277 at HEAD**;
  every file this story touched is equal or lower than its HEAD count. The one
  new finding is `envelope.py:49 A003 class attribute "type" is shadowing a
  Python builtin` — the key name is fixed by the spec's `GatewayError`
  contract at `:2996` and cannot be renamed. Left in S25's baseline rather
  than suppressed with a `# noqa`.
- `python -m build` + `twine check` → both artifacts `PASSED`.
- Clean venv, wheel installed, `from async_gateway.async_gateway import
  request` from a neutral cwd → `import OK`, and the timezone library is
  absent from the resolved set.
- **`fail_under` raised `0` → `71.93`**, from a measured
  `coverage report --precision=6` total of `71.936759%`.

**Open — a Definition-of-Done item this story's file boundary cannot satisfy.**

Four of the acceptance greps still match, and every remaining match is in
`logic/ftp_client.py` or `logic/sftp_client.py`, which are outside S7's
declared file boundary and are rewritten by S10, S11 and S12:

| Grep | Residue |
|---|---|
| `999` | `ftp_client.py:69`, `sftp_client.py:63` |
| `'tat'` | `sftp_client.py:59` |
| `except Exception` | `ftp_client.py:68`, `sftp_client.py:62` |
| `time.time()` | `ftp_client.py:70`, `sftp_client.py:59,64` |

S10's own DoD still speaks of "a successful delete is a success, not a `999` a
retry re-attempts", which confirms the occurrences are expected to survive
Step 5. The story-level DoD lifted R8/R9/R10's *release* acceptance criteria
verbatim without accounting for that, so the criterion is unsatisfiable here
rather than unmet.

Handled with the project's own established pattern for this situation (the one
S9 uses for its per-protocol key-set assertions): a package-wide scan test
marked `xfail(strict=True)` naming the two files and the stories that own them,
so the ratchet stays honest and each protocol story turns it green by deleting
its own lines — paired with a **non-xfail containment test** asserting the
residue is confined to exactly those two files, which fails the moment a banned
pattern appears anywhere else. Escalated rather than worked around.

**Files touched** (`git diff --name-only` plus untracked):

```
async_gateway/__init__.py
async_gateway/async_gateway.py
async_gateway/helpers/common/date_helper.py
async_gateway/helpers/internal/base.py
async_gateway/helpers/internal/request_helper.py
async_gateway/logic/http_client.py
async_gateway/utils/constants.py
async_gateway/utils/exceptions.py
async_gateway/utils/http_file_config.py      (Ruling F override)
pyproject.toml
async_gateway/utils/envelope.py              (new)
async_gateway/utils/redaction.py             (new)
async_gateway/utils/status_map.py            (new)
tests/test_envelope.py                       (new)
tests/test_exceptions.py                     (new)
```

---

## Work Log — code-review fix iteration 1 of 5

Verdict addressed: **CHANGES REQUESTED**, one High plus two Lows. No
re-implementation; three surgical edits and their tests.

### High — the redactor's caller extension point was unreachable

`redact_url` accepted `extra_params` and a unit test exercised it, but no
production call path supplied it: `envelope.py` called `redact_url(url)` with
no slot to pass one, and `log_failure` had the identical blind spot at
`redact_value(url)`. A caller whose secret rides in a non-default parameter
name (`?session_id=`, a vendor `?sig_v2=`) got it echoed verbatim in the
envelope's `url` **and** written to the log record — the exact leak the shared
redactor exists to prevent. Spec requires the extension point at
`v1_release_spec.md:713-716` and `:3464-3466`, and requires the envelope and
the log to agree at `:3848-3852`.

Threaded, not patched at two call sites. `request()` is the one place caller
configuration is read, so it is the one place it is normalised
(`async_gateway.py:170-171`), and the resulting set is handed to **both**
consumers — `new_envelope(..., redact_query_params=...)` at
`async_gateway.py:173-178` and `log_failure(..., redact_query_params)` at
`async_gateway.py:207`. The two therefore share one set by construction rather
than by two call sites happening to agree.

- `redaction.py:59-90` — new `normalise_param_names`, the boundary that copes
  with caller input being the wrong shape. Fails safe both ways: never raises
  (a typo in configuration must not kill a request) and never returns fewer
  names than given, since the result is *unioned* with `SENSITIVE_NAMES` and
  so can only mask more. A bare string is one name, not an iterable of
  characters — what the caller who wrote `'session_id'` meant.
- `redaction.py:205-238` — `redact_value` gained `extra_params`, forwarded to
  `redact_url` on the URL branch. Keeps the logger on the one dispatcher the
  spec's logging contract names, rather than special-casing the URL.
- `envelope.py:101-144` — `new_envelope` gained `redact_query_params`.

### Low 1 — `BasicAuth` at the depth bound

`_redact_recursive`'s `depth <= 0` early return sat above the `BasicAuth`
check, so an auth object *at* the bound was echoed verbatim. The bound exists
to stop matching **key names** — a `BasicAuth` needs no key name to be
recognised, so there was nothing for the bound to protect against. Type check
moved above the guard (`redaction.py:261-264`). Deeper than the bound the
documented limit still applies, and the new test says so explicitly rather
than implying a guarantee the function does not make.

### Low 2 — missing return annotation

`AsyncGatewayError.__init__` now declares `-> None`
(`exceptions.py:41-45`), per `.claude/rules/documentation.md` §4. mypy does
not flag it under current settings, which is why it survived.

### Out of scope, recorded not fixed

`redaction.py:136-139` gained a one-line docstring note that `parse_qsl`
splits on `&` only — the stdlib default is the hardened reading (a legacy `;`
is masked *with* the preceding value rather than escaping as a name of its
own). Documented residual, no code change. Everything else the reviewer raised
routed to its owning story untouched: `request_helper.py:45-46` (R14/Step 13),
`http_client.py:246` (M8/R13, Step 11), `README.md:238` (S29),
`redaction.py` urlencode re-encoding and `exceptions.py:24` `Final[int]`
(cosmetic). The pre-dispatch `ConfigurationError` emitting no log record
(`async_gateway.py:181`, vs the failure table's `Logged: error` at spec
:3815-3818) is a genuine spec contradiction and is raised as an open question,
**not** resolved by guessing.

### Tests added (7, all named for the behaviour they prove)

RED first: the five leak/ordering tests failed before the change and pass
after. The two malformed-input tests passed throughout — they are guards that
the defaults cannot be lost, and they would have failed had the normaliser
raised.

- `test_a_caller_supplied_parameter_name_is_masked_in_the_envelope`
- `test_r10_a_caller_supplied_parameter_name_is_masked_in_the_log` — asserts
  on the emitted `LogRecord.url`, not on a mock
- `test_caller_supplied_names_extend_the_defaults_they_do_not_replace`
- `test_a_caller_supplied_parameter_name_is_matched_case_insensitively`
- `test_a_malformed_redact_query_params_never_costs_the_defaults` — 6 cases:
  `None`, empty list, bare string, non-string elements, non-iterable, mapping
- `test_normalise_param_names_fails_safe_on_every_shape` — same 6 shapes at
  the boundary
- `test_a_basic_auth_object_at_the_depth_bound_is_still_masked` — verified
  load-bearing by reverting the ordering and confirming it goes red

### Verification

```
pytest                        175 passed, 1 xfailed   (158 + 1 before)
coverage --precision=6        72.551546%              (71.936759% before)
mypy async_gateway            Success: no issues found in 24 source files
mypy async_gateway tests      Success: no issues found in 35 source files
flake8                        no new finding in any touched file
```

`fail_under` **raised** 71.93 → 72.55 with the measurement recorded in
`pyproject.toml:114-124`. Never lowered; the CI ratchet stays satisfied.

**Files touched this iteration**

```
async_gateway/async_gateway.py
async_gateway/utils/envelope.py
async_gateway/utils/exceptions.py
async_gateway/utils/redaction.py
pyproject.toml                               (fail_under ratchet only)
tests/test_envelope.py
docs/project/tickets/AGW-7-...md             (this entry)
```

Not committed — the orchestrator commits after re-validating.

---

## Work Log — code-review fix iteration 2 of 5

### High (re-opened) — the exception-message channel bypassed the caller's set

Iteration 1 threaded `protocol_info['redact_query_params']` into the envelope's
`url` (`utils/envelope.py:130`) and the log `extra` (`async_gateway.py:70`),
but three sites built **exception messages** with `redact_url(...)` on defaults
only, so a caller-declared parameter name was masked in `envelope['url']` and
echoed in the clear a few keys away in `error['message']`. Reproduced on an
ordinary 404 — the commonest failure a consumer handles:

```
envelope url      : https://host/missing?session_id=***redacted***
error['message']  : GET https://host/missing?session_id=SESSIONSECRET returned HTTP 404
SECRET in repr(result)? -> True
```

The message reaches the caller through two channels, which is why patching
`finalise_error` would not have been enough: `finalise_error` copies it into
`error['message']` (`utils/envelope.py:198`), **and** `log_failure` emits with
`exc_info=True`, so the raw message is also in the rendered traceback.
Violates the shared-redactor contract (`docs/specs/v1_release_spec.md:3848-3852`)
and invariant **E9** (`:713-716`).

**Fix — redact at the raise site, with the caller's set.** The set is still
normalised exactly once, in `request()` (`async_gateway.py:176-177`); this
iteration only *routes* that one set to the places that interpolate a URL. No
site re-parses `protocol_info` — two independent normalisations is the bug
pattern the finding is about.

Two seams, both the smallest that reach their call sites:

1. **`BaseRequestClass.__init__`** takes a required keyword-only
   `redact_params: Collection[Text]` and stores `self.redact_params`
   (`helpers/internal/base.py:23-53`). Required rather than defaulted, because
   a defaulted redaction set is precisely how this leak survived iteration 1.
   Every protocol client subclasses with `*args, **kwargs`, so no subclass
   signature changes — S10/S11/S12 inherit the attribute for free.
   `request()` passes it at the one construction site
   (`async_gateway.py:199-201`).
2. **`make_http_request`** takes a required keyword-only
   `redact_params: Collection[Text]` (`helpers/internal/request_helper.py:117-124`).
   That module has no `protocol_info` and must not grow one. The value travels
   the existing `**kwargs` chain already used for `certificate`, `verify_ssl`,
   `headers` and `payload` — `handle_http_request` → filter method →
   `circuit_breaker.failsafe.run` — so the three intermediate signatures are
   untouched; the requirement at the leaf makes a dropped hop a loud
   `TypeError` rather than a silent fallback to the built-in names.

Redaction call sites changed:

- `logic/http_client.py:207` — `CircuitOpenError` message
- `logic/http_client.py:221` — `HttpStatusError` message
- `helpers/internal/request_helper.py:198` — `SerializationError` message
- `logic/http_client.py:204` — passes `redact_params=self.redact_params` into
  `handle_http_request`
- `async_gateway.py:69-70` — `redact_value(protocol, ...)` now also carries the
  set, so its safety no longer rests on the protocol guard at `:186` happening
  to reject a URL-shaped protocol first

**Grep proof.** Every `redact_url(` / `redact_value(` call in `async_gateway/`
is either fed the caller's set or provably cannot carry caller data. The three
remaining bare `redact_value(` calls are `exc.code` (this library's own
wire-stable constant), `envelope['status_code']` (int) and `envelope['latency']`
(float) — `extra_params` is consumed only by the absolute-URL branch of
`redact_value`, so it is inert for all three; noted inline at
`async_gateway.py:71-74`. `logic/ftp_client.py` and `logic/sftp_client.py`
contain no redactor call at all and interpolate no URL into any message (their
redaction is S10/S11/S12).

### Low — an assertion narrower than its own docstring

`tests/test_envelope.py:1119` asserted only `records[0].url` while claiming the
log and the envelope share one set. That gap is exactly why the three sites
above survived iteration 1: the claim was right, the assertion could not see
the channel that broke it.

- **Strengthened** `test_r10_a_caller_supplied_parameter_name_is_masked_in_the_log`
  to assert over the whole rendered record — message, `extra` **and** the
  `exc_info` traceback — via a new `render_record` helper
  (`tests/test_envelope.py:195-212`).
- **Added** `test_r10_a_caller_declared_secret_reaches_no_surface_on_a_404`:
  a real 404 through the normal path with a caller-declared parameter name,
  asserting the raw secret appears in **none** of `repr(result)`,
  `envelope['url']`, `error['message']`, `error['cause']` or the rendered log
  record — absence across the whole surface, not one field — plus that the
  sentinel *is* present, so a redactor that dropped the parameter entirely
  cannot pass.

Both were verified to bite: with the three raise sites reverted to bare
`redact_url(...)` and the plumbing left in place, both go red naming
`repr(result)` and the log record; restored, both pass.

### Out of scope, untouched (unchanged from iteration 1)

`utils/redaction.py:87-88` (Cosmetic, caller-pathological iterable) ·
`utils/envelope.py:50` flake8 `A003` (spec-mandated `error['type']`, S25) ·
`logic/ftp_client.py`, `logic/sftp_client.py` (S10/S11/S12) · `README.md` (S29)
· `logic/http_client.py`'s `json={}` (S13/Step 11) · the `async_gateway.py:161`
unlogged pre-dispatch raise (open spec question) · `.gitignore`.

### Verification

```
pytest                        176 passed, 1 xfailed   (175 + 1 before)
coverage --precision=6        72.657253%              (72.551546% before)
mypy async_gateway tests      Success: no issues found in 35 source files
flake8 (changed files)        18 findings, all pre-existing lines, 0 new
```

`fail_under` **raised** 72.55 → 72.65, truncated from the measured 72.657253%
per the convention recorded at `pyproject.toml:114-124`. Never lowered.

**Files touched this iteration**

```
async_gateway/async_gateway.py
async_gateway/helpers/internal/base.py
async_gateway/helpers/internal/request_helper.py
async_gateway/logic/http_client.py
pyproject.toml                               (fail_under ratchet only)
tests/test_envelope.py
tests/helpers/test_filters_helper.py         (2 direct-construction sites)
docs/project/tickets/AGW-7-...md             (this entry)
```

Not committed — the orchestrator commits after re-validating.

---

## Work Log — code-review fix iteration 3 of 5

### High (re-opened, third time) — foreign exception text, fixed at the seam

Iterations 1 and 2 each closed a *call site*. This one closes the seam, because
channel 4 is different in kind: it is text this library does **not** author.
`unwrap_cause` returned `str()` of a third-party exception verbatim, and
aiohttp's `InvalidUrlClientError`, `NonHttpUrlClientError`,
`NonHttpUrlRedirectClientError` and the `ClientResponseError` family all
stringify to the full URL **including the query string**. Reproduced against
the pre-fix tree:

```
message: RetriesExhausted: ftpx://host/p?session_id=SESSIONSECRET&api_key=APIKEYSECRET
cause  : NonHttpUrlClientError: ftpx://host/p?session_id=SESSIONSECRET&api_key=APIKEYSECRET
caller-declared secret leaked? True
DEFAULT api_key leaked?        True
```

The second line is the important one: the built-in `api_key` leaked too, so
this was a breach of **base E9** (spec:713-716) present with or without the
`redact_query_params` extension — not a failure of the extension feature.

**The invariant, stated in the code** (`utils/exceptions.py:19-29`): *no foreign
exception text leaves this module without passing through the redactor.* It
holds by construction, not by agreement between call sites:

- `unwrap_cause` (`utils/exceptions.py:210-263`) redacts **both** returned
  strings, **unconditionally**. Redaction takes no argument to switch on: the
  built-in `SENSITIVE_NAMES` are the base contract. A future third consumer
  that knows nothing about redaction therefore inherits the guarantee by
  calling the function — there is no unredacted way out of it. The optional
  `redact_params` keyword only ever *widens* the set.
- `redact_text` (`utils/redaction.py:191-227`) is the new redactor it uses. It
  masks a URL appearing **anywhere inside** a string, which `redact_url` cannot:
  the leaking text is a *sentence* (`'RetriesExhausted: ftpx://…'`), so the
  whole-string `_is_absolute_url` test used by `redact_value` sees nothing to
  mask. Text containing no URL is returned unchanged, so it is safe to apply to
  every message rather than to ones suspected of carrying one.
- The two existing consumers pass the caller's set to widen it:
  `finalise_error` gains a `redact_query_params` parameter
  (`utils/envelope.py:167-210`, fed from `async_gateway.py:234-236` where the
  set is already in scope — an explicit parameter, **not** a private key on the
  envelope, which is the caller-facing contract) and `transport_error_for`
  gains `redact_params` (`logic/http_client.py:113-153`, fed from
  `self.redact_params` at the two catch sites). The set is still normalised
  exactly once, in `request()` (`async_gateway.py:196-197`).
- `unwrap_cause`'s existing guarantees are intact: never-empty (E3 — redaction
  only ever rewrites the inside of a URL and both fallbacks are type names),
  depth-bounded, cycle-safe. The 14 tests pinning them are unchanged and green.

### Channel 5, found while proving channel 4 — the `exc_info` traceback

The required test asserts over the rendered log record *including the
`exc_info` traceback*, so it was measured rather than assumed:

```
SESSIONSECRET in rendered record? True      (with unwrap_cause already fixed)
APIKEYSECRET  in rendered record? True
```

`log_failure` emitted `exc_info=True`; the record's traceback is formatted by
whichever handler the **application** installed, from the live exception
objects — and the chained `NonHttpUrlClientError` at the bottom stringifies to
the unredacted URL. Nothing downstream can mask that. So `log_failure`
(`async_gateway.py:76-94`) now renders the traceback itself, passes it through
`redact_text`, and carries it as `extra['traceback']`.

**Cost, recorded not hidden:** a handler reading `record.exc_info` now finds
`None`; the full traceback *text* is in `extra['traceback']`. Accepted because
a credential in a log is auto-Critical and this is the only way to mask text a
third-party handler renders. The two tests that asserted `exc_info is not None`
(`tests/test_envelope.py:1072,1091`) now assert the exception type appears in
`records[0].traceback` — same intent, the channel that now carries it.

### Low — the surface guard that proved nothing

`test_r10_a_caller_declared_secret_reaches_no_surface_on_a_404` exercises only
the 404 / `HttpStatusError` raise site, where the chain is a single exception
*this library* raised with an already-redacted message and `error['cause']` is
`None` — so its `error['cause']` assertion ran against the string `'None'`.
Added, both driving `transport_error_for` with a real chained aiohttp
exception (scheme `ftpx://`, refused before a connection is opened, so no
server is needed and the exception is genuine, not a stand-in):

- `test_r10_a_transport_failure_leaks_no_foreign_exception_text` — asserts the
  secret is absent from `repr(result)`, `error['message']`, `error['cause']`
  and the rendered log record incl. the traceback; asserts
  `'NonHttpUrlClientError' in error['cause']` first, so the test cannot pass
  against a message this library wrote; asserts the sentinel *is* present, so a
  redactor that dropped the parameter cannot pass.
- `test_r10_a_default_secret_is_masked_with_no_redact_query_params` — **no
  `redact_query_params` supplied at all**, proving the base contract. Also
  asserts the *undeclared* `session_id` is still echoed, so the two halves of
  the set stay distinct rather than the query string being blanked wholesale.

Plus, pinning the seam itself rather than a path to it:
`test_unwrap_cause_redacts_foreign_text_without_being_asked` and
`test_unwrap_cause_masks_a_caller_declared_name_when_given_one`
(`tests/test_exceptions.py`), and two `redact_text` units in
`tests/test_envelope.py` for the embedded-URL and no-URL-is-a-no-op cases.

**Proved to bite.** With `unwrap_cause`'s redaction and the traceback rendering
reverted (mutation asserted landed by grep, then restored and re-asserted), the
four behaviour tests go red naming `repr(result)` and the message; the two
`redact_text` units stay green, as expected for a function the mutation did not
touch:

```
4 failed, 1 passed        (reverted)
6 passed in 0.12s         (restored)
```

### Out of scope, untouched

`redaction.py:237-238` (`redact_value` dropping `extra_params` for mappings and
sequences — Cosmetic, no call site) · `redaction.py:87-88` · `envelope.py:50`
`A003` · the `parse_qsl` `;` residual · `logic/ftp_client.py`,
`logic/sftp_client.py` (S10/S11/S12) · `http_client.py:246` `json={}` (S13) ·
`README.md` (S29) · the unlogged pre-dispatch raise at `async_gateway.py:161`
(open spec question) · `.gitignore`.

### Verification

```
pytest                        182 passed, 1 xfailed   (176 + 1 before)
coverage --precision=6        72.900763%              (72.657253% before)
mypy .                        Success: no issues found in 36 source files
flake8 (changed files)        1 finding: envelope.py:50 A003, pre-existing
                              and explicitly out of scope; 0 new
```

`fail_under` **raised** 72.65 → 72.90, truncated from the measured 72.900763%
per the convention at `pyproject.toml:114-125`. Never lowered.

### Residual

A foreign exception whose text carries a credential **not inside a URL** —
a bare token in a message body — is still echoed. Masking that needs a
value-shaped heuristic rather than a name-shaped one, which would mask
legitimate diagnostics; it is not in E9's stated bound
(`utils/redaction.py:12-18`). No such exception is known on the HTTP path.

**Files touched this iteration**

```
async_gateway/async_gateway.py
async_gateway/utils/envelope.py
async_gateway/utils/exceptions.py
async_gateway/utils/redaction.py
async_gateway/logic/http_client.py
pyproject.toml                               (fail_under ratchet only)
tests/test_envelope.py
tests/test_exceptions.py
docs/project/tickets/AGW-7-...md             (this entry)
```

Not committed — the orchestrator commits after re-validating.
