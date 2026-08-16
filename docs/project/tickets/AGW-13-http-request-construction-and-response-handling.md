# AGW-13: HTTP request construction and response handling

- **Status:** OPEN
- **Story:** S13 — spec Step 11, size **L** (`docs/specs/v1_release_stories.md` §4, Phase 2; sizing exception §12 — one step's worth of a single requirement pair over a fixed file set)
- **Spec:** `docs/specs/v1_release_spec.md` — R12 all, R13 all (Group E — HTTP protocol correctness)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; the request/response filter table
- **Decisions:** none
- **Files (declared scope):** ~`helpers/internal/{filters_helper,__init__,request_helper,response_helper}.py`, `logic/http_client.py` · +`tests/logic/test_http_client.py`, `tests/helpers/test_request_helper.py` · ~`tests/helpers/test_filters_helper.py`

## Why

R12 requires request construction that dispatches on the real Content-Type and never mutates the
caller's data; R13 requires response handling that distinguishes empty from broken. Today the same
media type is matched by exact key on one side and by substring on the other, unknown types route to
the JSON encoder (which is what sends a SOAP envelope through a JSON serializer), and the filters read
`kwargs['request_type']` — a hard `KeyError` for every SOAP call. The new raw-body branch added here is
the seam AGW-22 dispatches its envelope bytes through.

Closes findings: H14, M5, M6, M7, M8, M9, M10, L8.

## Definition of Done

- media-type dispatch strips parameters and is case-insensitive; **the same helper serves request and response sides** — the exact-key vs substring inconsistency *is* the evidence of the bug and must not survive
- **the spec's filter table exactly**, incl. the **new raw-body branch** for `text/xml`/`application/soap+xml`/`application/xml`; the `'default' → JSON` entry is **replaced** (unknown + `str`/`bytes` → raw, unknown + `dict` → JSON) — routing unknown types to JSON is what sends a SOAP envelope through a JSON encoder
- filters take a typed parameter set, **never `kwargs['request_type']`** (today a hard `KeyError` for every SOAP call)
- `grep -n "mapping.get(.*)("` shows no un-guarded immediate call
- `"get"`/`"GET"`/`"Get"` all attach the payload as **query params**
- bool → `"true"`/`"false"` via `isinstance`; `None`/`list`/`datetime`/nested each covered
- **no caller dict mutated** (byte-identical after the call)
- download keys have documented defaults; omitting both still downloads
- dead `if …: pass` removed and the GET+upload case decided explicitly
- malformed JSON → `json=None` + `ok=False` + `SERIALIZATION`, legitimate `{}` → `json={}` + `ok=True`, and a literal `null` distinguishable from a parse failure
- `UnicodeDecodeError` sets **both** `text` and `error`, no `KeyError` escapes
- multipart writes **bytes to a binary file** (`\x89PNG`, not `b'\\x89PNG'`), reads a `chunk_size*3+7` part in full, terminates cleanly on a `None` part, and accumulates into a buffer not `data + str(data)`

## Dependencies

- **blockedBy:** AGW-10
- **blocks:** AGW-14

## Decisions

- **D1 — the shared matcher lives in `helpers/internal/__init__.py`.** `normalise_media_type` /
  `media_type_of` / `is_json_media_type` / `filter_for_media_type`. Both the request dispatch
  (`request_helper.make_http_filters_without_file`) and the response parse
  (`http_client._copy_into_envelope`) call it, so the exact-key-vs-substring split cannot recur.
- **D2 — `+json` (RFC 6839) is accepted as JSON on both sides.** Exact-`application/json` matching
  alone would have *lost* `application/problem+json` parsing, which the old substring matching
  did handle. Keeping it makes the unification a fix rather than a trade. `+xml` is deliberately
  **not** added: the spec names three XML types and there is no existing behaviour to preserve.
- **D3 — `HttpResult.body` and `HttpResult.redirect_chain` are removed, not wired** (S7
  carry-forward 1). Nothing read either. `body` was a second full copy of the payload — exactly
  the duplication R14 sets out to bound — and `redirect_chain` was read off `resp.history`, which
  AGW-15's owned redirect loop replaces wholesale. AGW-15 reintroduces the hop record as its own
  loop's output, from the only source that will then be correct.
- **D4 — a body failure is *returned* from `_copy_into_envelope`, not raised there.**
  `SerializationError` carries its own 502; raised mid-copy it would overwrite the status the
  remote actually sent, so a 404 with a truncated body would reach the caller as a 502. The status
  is judged first, then the body error is raised.
- **D5 — the decode diagnostic travels as an optional `HttpResult['decode_error']`, not as a
  raise.** Raising from inside `failsafe.run` would have the breaker *retry* an undecodable body
  as though the network had failed, and would lose the lossily-decoded `text` R13 requires be set.
  Modelled as a `total=False` base class because `typing.NotRequired` is 3.11+ and the package
  supports 3.10.
- **D6 — an *empty* `http_file_download_config` now means "download on every default".** Absence
  (`None`) is the only "no download" signal. R12 requires that omitting both documented keys still
  downloads, and under the old truthiness check `{}` was silently ignored.
- **D7 — GET + `http_file_upload_config` is rejected with `ConfigurationError`** in
  `handle_http_request`, the one place that chooses the upload path. Dropping a file the caller
  asked to send is the only outcome that is silently wrong.
- **D8 — multipart stays on synchronous `open(..., 'wb')`.** R13 owns *binary*; R20 (Step 12) owns
  the `aiofiles` conversion and names this exact site. Converting here would put a line in this
  diff that traces to neither R12 nor R13.

## Work Log

### 2026-08-17 — implementation (lane α, worktree `/tmp/agw-wt-s13`, branch `lane/s13`)

Source changed:

- `helpers/internal/__init__.py` — replaced the exact-key `header_filter_mapping` and the
  `header_response_mapping` with the shared matcher and `filter_for_media_type`; the
  `'default' → JSON` entry is gone (H14).
- `helpers/internal/filters_helper.py` — `is_get` (M5), `coerce_query_value` /
  `build_query_params` (M6, copy-not-mutate), new `raw_body_filters`, and a uniform
  `(data, *, request_type)` signature on all three filters. Dead `if …: pass` removed (L8).
- `helpers/internal/request_helper.py` — multipart rewritten: binary file, parts read to
  completion, `None`-part guard, list accumulator (M7); `decode_error` instead of a raise (M9);
  `DEFAULT_DOWNLOAD_FILEPATH` and `CHUNK_SIZE_CONSTANT` defaults (M10); GET+upload rejected (L8);
  `HttpResult` slimmed (D3).
- `helpers/internal/response_helper.py` — empty → `None`, malformed → `SerializationError` (M8).
- `logic/http_client.py` — response side on the shared matcher, status-before-body-error
  precedence (D4), `http_file_download_config` default dropped (D6).

Tests added: `tests/logic/test_http_client.py` (19), `tests/helpers/test_request_helper.py` (18),
`tests/helpers/test_filters_helper.py` (+53, total 81). All against the real loopback
`aiohttp.web` recording server.

Verification (venv `/tmp/agw-s13`, at the worktree):

- `pytest --ignore=tests/helpers/test_response_helper.py` → **443 passed, 5 xfailed**, coverage
  **86.716558%** (floor 82.89). Stable across seeds `1`, `424242` and the default random order.
- `mypy async_gateway` → **Success: no issues found in 24 source files**.
- `flake8 async_gateway tests` → **28** findings, all pre-existing in `async_gateway/`
  (baseline was 32; four were removed incidentally on lines this story rewrote). `tests/` clean.

**BLOCKED — one file outside the declared scope.** `tests/helpers/test_response_helper.py` is not
in this story's boundary, and its
`test_an_undecodable_body_becomes_an_empty_mapping` pins the exact behaviour R13 forbids: it
asserts `''`, `b''`, `'not json at all'` and `b'{"unterminated": '` all yield `{}`. That
assertion *is* M8. Four parametrised cases fail on the full run; the other three tests in the file
pass unchanged. Proposed replacement, for whoever owns the file:

```python
@pytest.mark.parametrize(
    'body',
    [pytest.param('', id='empty-text'), pytest.param(b'', id='empty-bytes')],
)
async def test_an_empty_body_decodes_to_none(body) -> None:
    """An absent body is absent, not an empty object (R13/M8)."""
    assert await application_json_response(body) is None


@pytest.mark.parametrize(
    'body',
    [
        pytest.param('not json at all', id='not-json'),
        pytest.param(b'{"unterminated": ', id='truncated'),
    ],
)
async def test_a_malformed_body_is_reported_as_malformed(body) -> None:
    """A broken body raises rather than masquerading as ``{}`` (R13/M8)."""
    with pytest.raises(SerializationError):
        await application_json_response(body)
```

Two further out-of-boundary observations, reported and **not** fixed:

1. `README.md` documents `http_file_download_config.download_filepath` as `"required"`. It is now
   optional with a documented default (`response.txt`), and `file_download_chunk_size` defaults to
   `CHUNK_SIZE_CONSTANT`. The README needs a line.
2. `tests/fixtures/protocol_transports.py:265` still constructs `HttpResult(body=…,
   redirect_chain=[])`. Harmless — a `TypedDict` call is a plain `dict` call at runtime and nothing
   reads the extra keys — but the two arguments are now dead.

### 2026-08-17 — boundary extension under Ruling S: unblock `test_response_helper.py`

**Authorisation.** The orchestrator issued **Ruling S**, binding: the file
`tests/helpers/test_response_helper.py` is added to this story's boundary. It is the *only* file
added; everything else the implementation pass touched stands as reviewed-pending. This entry
records the extension so the diff reads as authorised rather than as scope creep.

**Why the extension was needed.** The file was created by S5 (the ujson → orjson migration,
`2d7bf40`), where its assertion "an undecodable body becomes `{}`" correctly pinned *parity* with
`ujson` — a migration whose job was to change nothing observable. R13 forbids exactly that
conflation, so the file pinned the very defect (M8) this story removes. No later story in the plan
declares the file: an ownership gap in the story breakdown, not a contradiction in the spec.

**Change.** `test_an_undecodable_body_becomes_an_empty_mapping` (4 parameters, all asserting `{}`)
is replaced by five tests that separate the outcomes R13 requires be distinguishable, each
asserting what the implementation actually does:

- `test_an_empty_body_is_none` — `''`, `b''` → `None`.
- `test_a_literal_null_is_none` — `'null'`, `b'null'` → `None` (an answer, not a failure).
- `test_a_legitimate_empty_object_decodes` — `'{}'`, `b'{}'` → `{}`, asserted `== {}` *and*
  `is not None`, since `{}` is falsey and a bare truthiness check would not catch losing it.
- `test_a_malformed_body_raises_serialization` — `'not json at all'`, `b'{"unterminated": '`,
  and `'   '` → `SerializationError` with `code == 'SERIALIZATION'`. The whitespace-only case is
  new: the helper's emptiness guard is a plain falsiness test, so a body of spaces takes the raise
  path, and that is worth pinning.
- `test_empty_and_broken_are_distinguishable` — R13's acceptance criterion asserted in one place,
  so the property is readable as one fact rather than inferred across the file.

No test was deleted, weakened, skipped or xfail-ed. The three tests carrying genuine S5 value —
`test_a_text_body_decodes`, `test_a_bytes_body_decodes`, and
`test_the_decode_error_is_still_a_value_error` — all survive. The last one's docstring is updated:
the `ValueError` subclassing is no longer what keeps the helper working (it now catches
`orjson.JSONDecodeError` by name), but it still pins the relationship that makes that narrowing a
strict tightening, and its failure mode is now "malformed cases raise `orjson.JSONDecodeError`
instead of `SerializationError`". The module docstring is rewritten for the same reason: left
as-is it stated the `{}` behaviour as a deliberate preserved property and would have contradicted
the tests beneath it. It now records that `{}` was S5-era parity that R13 replaced, so the
archaeology does not have to be repeated.

**Verification — full suite, no `--ignore`, no exclusions** (venv `/tmp/agw-s13`, at the worktree):

- `python -m pytest` → **456 passed, 5 xfailed, 0 failed**, coverage **86.72%** (floor 82.89).
  The 5 xfails are pre-existing in `tests/test_envelope.py`, owned by S12 and S22; none is mine.
- `python -m mypy async_gateway` → **Success: no issues found in 24 source files**.
- `python -m flake8 async_gateway tests` → **28** findings, unchanged from the pre-change count and
  below the 32 baseline. All 28 are pre-existing in `async_gateway/`; `tests/` is clean.

**Open question answered — `tests/fixtures/protocol_transports.py:265`.** Green on the full
un-ignored run; not edited, and it remains outside the boundary even as extended. `HttpResult` is a
`TypedDict`, so the extra `body=` / `redirect_chain=` keyword arguments are an ordinary `dict` call
at runtime and nothing reads them, and `mypy` runs over `async_gateway` only, so the dead arguments
are not reported there either. They stay dead code for whichever story owns the fixtures.

### 2026-08-17 — code-review iteration 2: fixes for Medium 1 and Lows 2, 4, 5

**Medium 1 (blocking) — the L8 `ConfigurationError` was raised too deep.** The GET-plus-upload
rejection lived in `handle_http_request` (`helpers/internal/request_helper.py:378`), *below* the
`except AsyncGatewayError` in `async_gateway.py:378`. `ConfigurationError` subclasses
`AsyncGatewayError` (`utils/exceptions.py:85`), so the raise was caught, converted into an
`ok=False` envelope and logged by `log_failure` — contradicting the entry point's own documented
contract (`async_gateway.py:298-311`) that a configuration error escapes synchronously, is reported
exactly once, is **not** also logged, and is raised **before anything is dispatched**. Every sibling
configuration error (unknown protocol, non-callable `serialization`, bad scheme) satisfies all
three; this one satisfied none.

- **Moved the check to construction.** New `validated_upload_config(config, request_type)` in
  `logic/http_client.py`, beside `validated_json_serializer`, called from `HttpRequest.__init__`
  where `self.http_file_upload_config` is read. `HttpRequest` is built at `async_gateway.py:368`,
  *outside* the `try`, so a raise there demonstrably escapes. The message and the case-insensitive
  `is_get` comparison are unchanged.
- **Removed the redundant deep check.** `handle_http_request` no longer validates the pair, and its
  now-unused `is_get` / `ConfigurationError` imports are gone. R11's rule is "validate once, at the
  boundary"; a boundary rule enforced in a transport helper was the redundancy. Its docstring records
  where the check went and why, so the removal does not read as a dropped requirement.
- **Re-asserted at the contract level.** `test_a_get_with_a_file_upload_config_is_refused` asserted
  `pytest.raises` against `HttpRequest.handle_request()` — *one layer below the conversion point*,
  where the raise was always real. That is why the test passed while the contract was broken: an
  assertion made at the level where the answer is the one you wanted. It is replaced by
  `test_a_get_with_a_file_upload_config_escapes_the_entry_point`, which drives the public
  `async_gateway.request()` and asserts all three documented properties together — it raises, no
  request reached the server, and zero `async_gateway` log records were emitted. A unit companion,
  `..._is_refused_at_construction`, pins that the rejection is the *constructor's*, which is what
  makes the escape a property of the design rather than a coincidence.
- **Confirmed RED first.** Both new tests failed `DID NOT RAISE ConfigurationError` against the
  pre-fix code — reproducing the reviewer's measurement — and pass after the move.

**Low 2 — a test docstring asserting an untestable mechanism.**
`test_a_bool_subclass_is_coerced_too` claimed to pin `isinstance(x, bool)` over
`type(x) in [bool]`. The claim is false: `bool` cannot be subclassed in CPython and `numpy.bool_`
derives from `np.generic`, not `bool`, so no value distinguishes the two spellings and the test
could not have. Its `Flag(int)` exercised the `int` branch. Renamed to
`test_an_int_subclass_renders_as_its_digits_not_as_a_bool`, with a docstring stating what it does
verify (bool is checked before int, because bool *is* an int) and stating plainly that the M6
`isinstance` sub-point is unfalsifiable in CPython. The same false clause in the source docstring
of `coerce_query_value` (`filters_helper.py:89-91`) is corrected identically — the ordering claim it
also makes is real and load-bearing and is kept.

**Low 4 — `application_json_response`'s return annotation.** `-> JsonBody` was inaccurate:
`JsonBody = Optional[dict[str, Any] | list[Any]]` (`utils/envelope.py:32`) names only the container
forms, but `b'42'` → `42`, `b'"hello"'` → `'hello'` and `b'true'` → `True` are all valid JSON bodies.
Made honest without touching `envelope.py`: a local `DecodedJsonBody` alias in `response_helper.py`
covering the scalar forms too. Not a regression — the previous annotation `-> Dict` was equally
wrong, and `pyproject.toml:105` (`ignore_errors = true`) means mypy sees neither.

**Low 5 — missing parameter annotations.** Annotated the parametrised parameters at
`tests/logic/test_http_client.py:309-311` and `tests/helpers/test_filters_helper.py:453-454,
484-485, 506-507, 560-562, 598-599, 695-696, 755-756`, plus `tmp_path: Path` on the upload tests.
`.claude/rules/documentation.md` requires full annotations and `test_response_helper.py` already
complied, so the suite was inconsistent with itself.

**Out of boundary — recorded, not fixed.** In addition to the two items logged in the previous entry
(both still open):

3. **`JsonBody` is too narrow** (`utils/envelope.py:32`). Scalar JSON bodies reach
   `GatewayResponse['json']`, which is typed `JsonBody`, so the envelope's own type understates its
   contents in exactly the way Low 4 describes. `response_helper.py` now annotates honestly with a
   local alias; the fix proper is to widen `JsonBody` to
   `Optional[dict[str, Any] | list[Any] | str | int | float | bool]`. **Defect for whichever story
   owns `utils/envelope.py`.** It is invisible to CI today because `pyproject.toml:105` sets
   `ignore_errors = true`; removing that (R27, Step 24) will surface it, so it should be fixed before
   or with R27 rather than discovered by it.
4. **README, for S29** (which owns the README rewrite): `README.md:167` still documents
   `http_file_download_config.download_filepath` as **"required"**, which M10 made false (it is
   optional, defaulting to `response.txt`) — this is item 1 above, restated here so S29 inherits it
   — and the new **GET + `http_file_upload_config` rejection is undocumented**. A caller reading the
   README today has no way to learn that the combination now raises `ConfigurationError`
   synchronously rather than being silently dropped or sent as a GET body.

**Verification — full suite, no `--ignore`, no exclusions** (venv `/tmp/agw-s13`, at the worktree):

- `python -m pytest` → **457 passed, 5 xfailed, 0 failed**, coverage **86.74%** (floor 82.89).
  One net new test (two added, one replaced). The 5 xfails are the same pre-existing ones owned by
  S12 and S22.
- `python -m mypy async_gateway` → **Success: no issues found in 24 source files**.
- `python -m flake8 async_gateway tests` → **27** findings, one *below* the 28 carried in, because
  splitting the over-long `self.http_file_upload_config = ...` assignment removed an existing E501.
  All 27 are pre-existing in `async_gateway/`; `tests/` is clean.
- **Contract proof, captured through the public entry point** — `request()` with
  `request_type='get'` and an `http_file_upload_config`:

  ```
  RAISED: ConfigurationError: http_file_upload_config cannot be combined with
    request_type 'get': a GET has no body to upload a file in. Use POST or PUT,
    or remove the upload config.
  log records emitted by async_gateway: 0 []
  ```

  Escapes as an exception rather than an envelope, and is not logged. Nothing was dispatched: the
  entry-point test asserts `not http_server.requests` against a live loopback server.

No test was weakened, deleted, skipped or xfail-ed. No `# noqa`, no `# type: ignore`, no bare
`except`. The ten mutation-proven behaviours cleared in iteration 1 are untouched.
