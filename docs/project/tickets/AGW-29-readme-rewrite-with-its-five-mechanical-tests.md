# AGW-29: README rewrite, with its five mechanical tests

- **Status:** OPEN
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

_Empty — opened at stage 1g, before implementation._
