# AGW-29: README rewrite, with its five mechanical tests

- **Status:** DONE
- **Story:** S29 — spec Step 28, size S (`docs/specs/v1_release_stories.md` §4, Phase 7)
- **Spec:** `docs/specs/v1_release_spec.md` — R29 all (Group N — Documentation) · R21-AC3,AC4 · R8-AC12 · R6-AC7 · R5-AC7 · R15-AC5 (doc half) · R16-AC6 · R18-AC9 · R25-AC6, AC7 (doc half) · R34-AC4
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; the public API surface
- **Decisions:** none — *reads OQ7's answer (the LICENSE copyright line, owned by AGW-28); not blocked on AGW-28's code*
- **Files (declared scope):** ~`README.md`, `tests/test_docs.py`

## Why

R29 requires a README written for the developer who is importing this library, and it is the
mechanical home for a set of criteria other requirements defer to it: R8's payload-echo disclosure,
R18's "MTOM documented as out of scope", R21's URL-trust contract, and R25's export documentation.
Every documented example is compiled, every documented import resolved, and every documented call
checked against `inspect.signature`, so the README cannot drift from the code it describes. The
`protocol_info`-keys-both-ways test is the anti-drift criterion — not the length.

Closes findings: H24, MG6, MG8.

## Definition of Done

- named sections: Install · Quickstart per protocol · Public API reference · The response envelope (R8's full key set, types, per-protocol `status_code`) · Error handling (R10's hierarchy + code table) · Retry/timeout/circuit-breaker · Transport security · **You own URL validation** · Supported Python versions · Versioning policy · Contributing · Changelog link. *(A deliberate, stated divergence from `documentation.md` §1's service-shaped template: this is a library with no endpoints, env vars, services or health check.)*
- **every ```python block `compile()`s**; **every documented `from async_gateway… import` resolves**; **every documented call matches `inspect.signature`**
- the payload-echo disclosure is present and tested (masked by key name to depth **4**; below that and for non-mapping payloads the caller's data is echoed **verbatim**) — this is the mechanical home for R8/E9's "and the README says so plainly"
- the SOAP section carries all **three** consumer facts, each asserted: hand-built XML string or `Element` with no dict-to-XML; `soap_body` is a raw `Element`, not a mapping, shown with one line of ElementTree; MTOM unsupported and raising — the mechanical home for R18's "documented as out of scope"
- `grep -c "api.fyndx1.de" README.md` → **0**
- `grep -ci "sftp"` > 0 with host-key verification, pinning, the named bypass and key auth documented
- `999` appears nowhere as a status
- a test asserts every `protocol_info` key the code reads appears in the README **and vice versa** (the anti-drift criterion — not the length)
- every allowlisted verb appears in the README and vice versa
- the release section names **one** file to bump
- the licence and attribution are stated

## Dependencies

- **blockedBy:** AGW-27
- **blocks:** AGW-32

*(Story note, §4: this story reads OQ7's answer but is **not** blocked on AGW-28's code — the two run
as parallel lanes in W22.)*

## Decisions

_None recorded yet._

## Inbound obligations from other stories

### From S18 / AGW-18 (R22, path containment) — landed

S18 did not edit `README.md`: this story owns that file. R22-AC3 and R22-AC6 both require a
statement **in the README**, so S18 discharges the code half and hands the prose half here. Four
facts, all of them now true of the shipped code:

- **Downloads refuse to overwrite by default.** `O_EXCL` is the default on every local write; an
  existing destination raises `ConfigurationError`. Callers re-downloading to a stable path pass
  `overwrite=True` — available on `download_file_from_url(...)`, in `http_file_download_config`,
  and in the FTP/SFTP `protocol_info`. **The README's own examples use fixed `/tmp/test.pdf` and
  `/tmp/temp.png`, which now fail on their second run unless the flag is passed** — so whatever
  those examples become, they must not silently teach the broken shape.
- **Downloaded files are created mode 0600**, not at whatever `umask` allows (M18).
- **A symbolic link at the destination is refused, never followed** — including with
  `overwrite=True`, which opts into replacing a file and not into following a link.
- **Platform degradation (R22-AC6).** Where `O_NOFOLLOW` is unavailable the write degrades to an
  explicit pre-write `lstat` check, which carries a TOCTOU window the flag does not. The criterion
  says this difference is documented; that sentence belongs in this README.

Two further consumer-visible facts worth a line, both from R22's Edge Cases: a relative
`local_filepath` resolves against the **process CWD**, and a destination that is itself a directory
is reported as a directory rather than advising `overwrite=True` (the flag cannot help there).

**Breaking change** (for the CHANGELOG, AGW-31/S31 — also recorded in AGW-18's work log): a second
download to an existing path previously succeeded and now raises `ConfigurationError` unless
`overwrite=True`.

## Work Log

### 2026-08-17 — implemented on `lane/s29`

`README.md` rewritten from scratch (1,126 → ~1,100 lines, but the length is not
the criterion — the anti-drift tests are). `tests/test_docs.py` created with 28
tests. Every fact below was read out of the current code, not out of the old
README: the `protocol_info` key set was extracted by walking the package's AST,
the exception hierarchy and its statuses were printed from
`utils/status_map.py`, and every signature was read with `inspect.signature`.

**Sections:** Install · Quickstart per protocol (HTTP/HTTPS, FTP, SFTP, SOAP) ·
Public API reference · The response envelope · Error handling · Retry, timeout
and circuit-breaker behaviour · Transport security · You own URL validation ·
Local files · Redaction and the payload echo · Logging · Supported Python
versions · Versioning policy · Contributing · Changelog · Licence and
attribution.

**The five mechanical checks**, each proven to fail when violated:

1. Every ```python block `compile()`s (with `PyCF_ALLOW_TOP_LEVEL_AWAIT`).
2. Every documented `from async_gateway… import` resolves.
3. Every documented signature listing is *executed* into a real function object
   and compared against `inspect.signature` parameter-by-parameter; every
   documented call is bound against the real signature.
4. **Every example runs** against the loopback `RecordingHTTPServer` and the
   FTP/SFTP transport doubles, with its own assertions intact.
5. Set checks **both ways** for `protocol_info` keys, HTTP verbs, FTP commands,
   SFTP modes, envelope keys, `GatewayError` keys, error codes, and the two
   resilience config key sets.

**Sabotage evidence** (each reverted immediately after):
`201`→`299` in the HTTP example → `test_every_example_runs` FAILED, naming
example #1. Dropping `head` from the verb table → `test_http_verbs_...` FAILED
with the exact diff. `latency`→`tat` in the envelope table →
`test_envelope_keys_...` FAILED "missing ['latency'], invented ['tat']".

**Inbound obligations, all discharged:** S18's local-write contract (overwrite
refused by default, `O_EXCL`/`O_NOFOLLOW`, mode 0600, symlink refusal, the
`O_NOFOLLOW`-absent TOCTOU degradation, relative-path CWD resolution, the
directory case) — and the old README's fixed `/tmp/test.pdf` examples are gone,
replaced by placeholders the test binds to `tmp_path`, with the second-run
failure documented as deliberate. S15's whole-chain redirect deadline. S7/OQ13's
redacted `extra['traceback']`, its APM-grouping cost and the one-line revert.
S19's seven-verb allowlist including `head`. S20's keyword-only
`download_file_from_s3` with `Filename=`, and `http_file_upload_config`
documented against the real `utils/http_file_config` module. S5: no `ujson`
anywhere; orjson's `bytes` return noted.

**Corrections made to my own first draft, both caught by the tests rather than
by review:** the payload-echo example masked at depth 3 and claimed depth 4 was
verbatim — the real boundary is *level 4 masked, level 5 verbatim*, measured
against `redact_payload`, and the example now shows both sides of it. The
`'HTTPS'`-under-plaintext-loopback problem is handled by rewriting the scheme
and the protocol name **as a pair**, leaving the deliberate `http://` +
`protocol='HTTPS'` refusal example untouched.

**Not verified against code (stated as-is):** the CHANGELOG link (AGW-31 owns
that file, which does not exist yet) and the `docs/decisions/` reference for the
pyfailsafe ADR (AGW-23's). The version bump section names `pyproject.toml` as
the single file per R5-AC7; `async_gateway.__version__` does not exist yet
(AGW-28 owns it), so the README describes the end state and does not assert it.

**Gate:** `pytest` 1132 passed, 98.41% (baseline 1104 / 98.41%);
`mypy async_gateway` clean; `flake8 async_gateway tests` 19 findings — the
unchanged pre-existing baseline, none in the new file.

**OQ7 read, not decided:** the LICENSE line is retained as `2022 Fynd` and the
README's attribution section states it verbatim, with the fork's own
contributors named alongside — the recommended reading. AGW-28 owns any change
to `LICENSE` itself; `test_the_licence_and_attribution_are_stated` reads the
holder *out of the LICENSE file*, so if AGW-28 changes that line this test fails
until the README follows.
