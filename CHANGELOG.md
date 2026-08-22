# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- First-class bounded GCS selector with `google-cloud-storage>=3,<4` for ADC-
  authenticated upload, download, head, one-page list, and V4 signed URLs.
- First-class JSON-RPC 2.0, GraphQL, guarded S3, and raw unary-unary gRPC
  selectors, all using the common response envelope, validation, redaction,
  retry, circuit-breaker, cancellation, and bounded-response contracts.
- Explicit FTP `tls_mode` support for verified implicit and explicit FTPS,
  while preserving the legacy omitted-mode behavior.
- Runnable loopback or deterministic-double examples for every new selector.
- Typed `SFTPAuth` for password, explicit client-key, or mixed SFTP
  authentication, with encrypted-key passphrases and explicit SSH-agent
  opt-in.
- Documentation, Source, Issues, Changelog, and Security project links in
  package metadata, plus the repository security policy.

### Changed

- Pre-processors may still enrich payload and metadata, but now raise
  `ProcessorError` if they change `url` or `protocol`; post-processors retain
  report-field editability.
- Unknown ordinary legacy `protocol_info` keys emit `DeprecationWarning` in
  1.x and will become errors in 2.0. Unknown security-sensitive keys fail
  immediately; the four new selector contracts reject every unknown key.

### Fixed

- Unknown top-level `request()` keywords now raise `ConfigurationError` before
  processors or network code instead of being silently ignored.
- SFTP key-only authentication no longer passes through the FTP validator that
  required a password.
- Package metadata now names all supported protocols rather than claiming
  unsupported Redis/XML protocols.

### Security

- SFTP passes `agent_path=None` unless `SFTPAuth.use_ssh_agent=True`, so
  explicit client keys cannot implicitly activate `SSH_AUTH_SOCK`.

## [1.0.0] — 2026-08-19

The first release **under this name**, and a near-total rewrite of the code it
inherited. Read the three notes below before the change list: they are what
make a release numbered *lower* than its predecessor safe, and what its
existing users need to do about it.

### About the rename, and the version going down from 2.7.3 to 1.0.0

**This code is published — under a different name.** It ships today as
[`asyncio-requests`](https://pypi.org/project/asyncio-requests/), currently at
`2.7.3` (uploaded 2023-01-02), with 12 releases going back to 2022-02-24 and
roughly 110 downloads a month. That distribution is the direct predecessor of
this one: same `request()` entry point, same `logic/{http,ftp,sftp,soap}.py`
layout, and `2.7.3` is the exact version this repository inherited in its
packaging metadata.

So this release is **a rename with a discontinued predecessor**, not a first
release of new code. `asyncio-requests` is retired at `2.7.3`; development
continues here as `asyncio-gateway`.

The version reset is safe, but not for the reason a first release would be
safe. It is safe because **the new distribution name has no history**:
`asyncio-gateway` has never been uploaded to PyPI, so no resolver can be
confused, no pin can be invalidated, and no `>=2.0` constraint can exist —
nobody can hold a requirement on a name that has never existed. Publishing
`asyncio-gateway 1.0.0` cannot move any installed package backwards, because no
installed package answers to that name.

What is *not* true is that this code has no users. It has them, on the old
name, and they are the subject of the next two notes.

### Why the name is `asyncio-gateway` and not `async-gateway`

The name intended throughout this rewrite was `async-gateway`, and it is the
name most of this repository's planning documents argue for. **PyPI rejected
it** — "This project name is too similar to an existing project" — and the
reason is worth recording, because the check that was run to clear the name
could never have caught it.

PyPI does not compare the *spelling* you type; it compares the
[PEP 503](https://peps.python.org/pep-0503/) **normalised** form,
`re.sub(r'[-_.]+', '-', name).lower()`. Under that rule `async-gateway`,
`async_gateway` and `asyncgateway` are all **one name**, and
[`asyncgateway`](https://pypi.org/project/asyncgateway/) — an unrelated
"Itential Gateway Async Client", `0.1.0`, uploaded 2026-03-09 — already
holds it. The `GET https://pypi.org/pypi/async-gateway/json` → 404 recorded
during planning was true and useless: a 404 on one spelling says nothing
about the normalised name it belongs to. (`aio-gateway` is blocked the same
way, by an existing `aiogateway`.)

`asyncio-gateway` was verified free in **both** forms before it was adopted:
`pypi.org/pypi/asyncio-gateway/json` and `pypi.org/pypi/asynciogateway/json`
each returned 404. The lesson is now mechanical rather than remembered —
`tests/test_packaging.py` pins the distribution's normalised form, so the
name that must be checked against PyPI is the one the test states.

### For existing `asyncio-requests` users

You are not carried along by a resolver. `pip install --upgrade
asyncio-requests` will not find this release, by design — a rename means the
migration is deliberate, which is the honest trade for not silently swapping a
package's import path and behaviour underneath a working program.

To migrate:

1. Replace the dependency: drop `asyncio-requests`, add `asyncio-gateway`.
2. Change the import path: `asyncio_requests` becomes `asyncio_gateway`. The
   entry point keeps its name — `asyncio_gateway.asyncio_gateway.request()`.
3. Work through the breaking changes listed below. They are real, and against
   `2.7.3` they are the changes you will actually feel: the single response
   envelope, the removal of `api_response`, `tat` becoming `latency`, the FTP
   `verify_ssl` default flip, SFTP host-key verification now on by default,
   and the `logic/*` module renames.

Staying on `asyncio-requests 2.7.3` is a choice to stay on the defects in the
advisory below. It will receive no further releases.

### Security advisory — `asyncio-requests <= 2.7.3`

The published predecessor carries three defects that this release fixes. They
are stated here because that distribution has current users who cannot see
this repository's history. No CVE has been requested or assigned for any of
them; the severities below are our own plain description of the impact, not a
scored rating.

- **SFTP host-key verification is disabled**
  (`asyncio_requests/logic/sftp.py:42`, `known_hosts=None`). Every SFTP
  connection accepts any host key without checking it, so a machine-in-the-
  middle on the network path can impersonate the server and read or alter the
  transferred file and the credentials used to fetch it. This is the one to
  act on first. In this release, host-key verification is **on** by default.
- **FTP raises on every call** (`asyncio_requests/logic/ftp.py:50-52`):
  `verify_ssl` is bound only inside an `if`, so any code path reaching the
  connect call hits `UnboundLocalError`. The FTP protocol has therefore never
  worked in a published release; the impact is a hard failure, not silent
  corruption.
- **SOAP is advertised but absent**
  (`asyncio_requests/logic/soap.py` is a zero-byte file). Requesting `'SOAP'`
  raises `TypeError: 'NoneType' object is not callable` rather than making a
  call. SOAP is implemented for real in this release.

Separately, `2.7.3` pins its dependencies at `aiohttp>=3.7.3`,
`ujson>=4.0.1`, `zeep[async]==4.0.0` and `aioboto3==8.0.5`. The two exact pins
in particular hold those packages at 2020–2021 releases, so an install
inherits whatever vulnerabilities have been reported against them since. This
release ships current, patched versions.

**Remedy:** migrate to `asyncio-gateway 1.0.0` as described above. If you cannot
migrate yet, treat SFTP through `asyncio-requests` as unauthenticated at the
host level and do not use it over an untrusted network; the FTP and SOAP paths
are non-functional and nothing depends on them.

### About the breaking changes

They are listed as breaking, plainly and in full, because that is what they
are — measured against `asyncio-requests 2.7.3`, the code and the release
both. Under the new name none of them can break an existing install, because
no existing install resolves to it; they are recorded so that anyone migrating
from `asyncio-requests` — or comparing this code against the old README, an
internal fork, or a vendored copy — can see exactly what moved, rather than
discovering it at runtime.

### Added

- **SOAP is implemented.** `logic/soap_client.py`: SOAP 1.1 and 1.2 envelope
  construction, version-correct transport headers, a hardened parse and Fault
  mapping into the one error contract. The old README advertised a SOAP client
  that **did not exist** — `logic/soap.py` was a zero-byte file and `'SOAP'`
  mapped to `None`, so asking for it raised
  `TypeError: 'NoneType' object is not callable`.
- **A circuit breaker that can actually open.** It was previously constructed
  per request, so every call met a zeroed failure count. It is now held in a
  **per-destination** registry, keyed on `(family, host, port)`, so one flaky
  host cannot open the circuit for every destination.
- **A library-owned redirect loop**, so the configured timeout bounds the whole
  chain instead of each hop, and a hop that crosses an origin is reduced to an
  allowlist of safe headers (see the breaking change below).
- **Path containment on every local write** (`utils/paths.py`,
  `utils/contained_io.py`), including the recursive FTP and SFTP directory
  transfers, where a hostile server-supplied filename could previously write
  outside the target directory.
- **Verb allowlists** for all five caller-named-operation sites, so no
  caller-supplied string reaches a bare `getattr` on a live transport client.
- **A response size cap** (`max_response_bytes`, default 64 MiB) and explicit
  `allowed_schemes`, `allow_redirects` and `max_redirects` controls.
- **Runnable examples** under `examples/`, imported and executed by the test
  suite so they cannot rot. They ship in neither the wheel nor the sdist.
- **This changelog**, plus a CI step that fails a pull request touching
  `asyncio_gateway/` without touching `CHANGELOG.md`.
- **A lint gate that is on and at zero.** `flake8 .` exits 0 with no output
  from a fresh `pip install -e '.[dev]'`. Two configuration defects were
  what previously made the command meaningless rather than merely noisy:
  `application_import_names` named `async-gateway`, which is not a legal
  module name, so every first-party import was classified third-party and
  import-order checking had been checking a fiction; and a bare `.` walked
  into the checked-in agent tooling under `.claude/`, which is not this
  library. Both are fixed and documented in `.flake8`. Two new CI steps
  hold the line: one fails any `# noqa` or `# type: ignore` that does not
  name the code it silences *and* carry a `-- why`, and one runs `flake8`
  in check mode over the formatting codes alone, since this project
  configures no autoformatter and `flake8` is the formatting judge.
- **A type gate that reports.** `ignore_errors = true` is gone from
  `pyproject.toml` and `mypy asyncio_gateway` exits 0 on the 24 real errors it
  had been turning into `Success: no issues found` — including a `"None" not
  callable` on the HTTP filter-method dispatch, four `Optional` attributes
  annotated as though they could not be None, and an envelope `json` type too
  narrow for the scalar bodies that reach it. Two latent defects surfaced with
  them and are fixed: a missing `remote_path` (SFTP) or `server_path` (FTP)
  reached the transport as an un-enveloped `TypeError` rather than the caller's
  `ConfigurationError` it is. `ignore_missing_imports` is now per-module, one
  override per package that genuinely ships no stubs, so the *next* untyped
  dependency is noticed rather than pre-silenced; a CI step fails either
  setting coming back.
- **`py.typed`** (PEP 561), so downstream type checkers see the annotations
  instead of ignoring the package. Added last, deliberately: the marker is a
  promise that this package's own types check, and publishing it earlier would
  have exported the errors above into every consumer's build. A packaging test
  asserts it ships in both the wheel and the sdist, because the failure has no
  runtime symptom at all.

### Changed — BREAKING

- **A cross-origin redirect now forwards only allowlisted headers.** A hop that
  crosses an origin boundary carries `Accept`, `Accept-Charset`,
  `Accept-Encoding`, `Accept-Language`, `Content-Type`, `Content-Length`,
  `Content-Encoding`, `Content-Language`, `Content-Disposition`, `Range`,
  `Cache-Control`, `Pragma` and `User-Agent`, and **drops everything else** —
  including any custom header of yours. Same-origin hops are unchanged and
  forward everything.

  This replaces a list of credential headers to *strip*, which was the wrong
  shape twice: naming the secrets requires having thought of all of them, and
  the header that leaks is the one nobody thought of. The strip-list forwarded
  `X-Api-Key` in its first form and, in its second — the union of every
  credential set in the codebase — still handed `X-Vault-Token`,
  `Private-Token`, `X-Goog-Api-Key`, `X-Access-Token`, `X-Auth-Key`,
  `X-Session-Token`, `X-Functions-Key`, `Dd-Api-Key`, `X-Shopify-Access-Token`
  and `Authentication` to a hostile host verbatim. Inverting to an allowlist
  fails closed, so a header this library has never heard of does not cross.

  **If you rely on a custom header surviving a cross-origin redirect**, name it
  in the new `protocol_info['cross_origin_headers']`. That key widens the
  allowlist into headers this library has no opinion about; it raises
  `ConfigurationError` rather than re-admitting one it recognises as a
  credential.

  A supplied `session` is now validated against the same allowlist rather than
  against the credential list, which closes the identical hole on that path:
  aiohttp merges session defaults into every request and no hop can withhold
  them, so a session carrying any header off the allowlist is refused unless it
  is declared in `cross_origin_headers`.
- **One response envelope, for every protocol and both outcomes.** `request()`
  now always returns the same key set — `ok`, `status_code`, `protocol`, `url`,
  `request_time`, `latency`, `payload`, `text`, `json`, `headers`, `cookies`,
  `error`, `protocol_details`, `request_tracer`, `pre_processor_response`,
  `post_processor_response`. Previously HTTP built a fresh dict that silently
  dropped several keys, FTP and SFTP **returned `True`**, and SOAP did not run.
  **`result['ok']` is now the only correct success check.**
- **`api_response` is gone.** The response body was previously nested under
  `response['api_response']`; its contents are now top-level keys of the
  envelope (`text`, `json`, `status_code`, `headers`, `cookies`).
- **`tat` is now `latency`**, and it is measured with a *monotonic* clock
  rather than `time.time()`, so a wall-clock adjustment can no longer make it
  negative. It is a `float` of seconds and is present on every response.
- **A failure no longer costs you the response.** An `ok=False` envelope keeps
  the `status_code`, `headers`, `cookies`, `text` and `json` the protocol
  received — a `404` with a JSON error body is fully readable. Previously every
  failure collapsed to a fabricated `999` with no body and no log line.
- **Errors are typed and carry a stable code.** `error` is
  `{type, code, message, cause}`, where `code` is one of a wire-stable set
  (`HTTP_STATUS`, `TIMEOUT`, `CONNECT`, `DNS`, `TLS`, `HOST_KEY`, `CONFIG`,
  `CIRCUIT_OPEN`, `SOAP_FAULT`, `PATH`, `SERIALIZATION`, …) and `message` is
  **never empty**. Library bugs — a `KeyError`, a `TypeError` in our own code —
  now propagate to you instead of being reported as a failed network call.
- **FTP `verify_ssl` now defaults to `True`.** It defaulted to `False`, sending
  `USER`/`PASS` in the clear on the one protocol that transmits them as literal
  commands. Relatedly, FTP had **never executed at all**: `verify_ssl` was read
  before assignment, so both branches raised `UnboundLocalError`. Passing
  `verify_ssl=False` still works and now logs a warning.
- **SFTP host-key verification is on by default.** `known_hosts=None` was
  hardcoded, so any server key was accepted on every session. The default is
  now asyncssh's own `known_hosts` resolution; a mismatch surfaces as
  `error['code'] == 'HOST_KEY'`. The bypass must be named:
  `insecure_skip_host_key_check=True`. A falsy `verify_ssl` does not enable it,
  and neither does the string `'false'`.
- **SFTP no longer offers ambient identities.** On-disk keys and the running
  `ssh-agent` are no longer presented unless you pass `client_keys` yourself.
- **The `logic/*` modules are renamed** to `logic/http_client.py`,
  `logic/ftp_client.py`, `logic/sftp_client.py` and `logic/soap_client.py`.
  `logic/http.py` shadowed the stdlib `http` package. No compatibility aliases
  are provided — there is nobody to be compatible with.
- **Local downloads refuse to overwrite by default.** A download to a path that
  already exists now raises `ConfigurationError` instead of silently replacing
  the file; pass `overwrite=True` to opt in. Downloaded files are created mode
  **0600** rather than at the process `umask`, and a **symbolic link at the
  destination is refused, never followed** — including with `overwrite=True`,
  which opts into replacing a file and not into following a link.
- **`download_file_from_s3` takes keyword-only arguments.** It could not
  previously have succeeded in either of the two copies it existed in: both
  called the module-level `aioboto3.client(...)` factory **removed in aioboto3
  9.0**, and both passed the destination as `file_save_path=` to
  `download_file`, whose keyword is `Filename=`. The duplicate in
  `helpers/common/file_helper.py` is deleted — its parameters were in a
  *different order*, so a caller who imported the wrong one and called
  positionally wrote their bucket name to a local path. Keyword-only arguments
  make that mistake unexpressible.
- **A URL download refuses every non-success status.** It previously checked
  only for `403`, so a `404` body, a `500` stack trace or an HTML login page
  was written out as the requested file.
- **The HTTP timeout bounds the whole redirect chain**, not each hop. A hostile
  `Location` header can no longer outspend your deadline.
- **`Content-Type` dispatch is case-insensitive and parameter-aware.**
  `application/json; charset=utf-8` previously matched nothing. `text/xml`,
  `application/xml` and `application/soap+xml` now route to a raw-body branch;
  an unknown media type with a `str` or `bytes` payload is sent raw rather than
  through the JSON encoder.
- **An empty body, a literal `null`, a legitimate `{}` and a malformed body are
  now four distinct outcomes.** They were previously two: a parse failure was
  indistinguishable from an empty object.
- **File uploads use `Transfer-Encoding: chunked`.** The upload body is now a
  streaming body built per attempt, so a retry re-sends the file instead of
  uploading zero bytes while the server answers `200`. A streaming body has no
  known length, so no `Content-Length` is declared — a stat-derived one would
  be a lie.
- **`delete_local_file_path` is idempotent**; it previously raised
  `FileNotFoundError` on an already-absent file.
- **The failure log carries `extra['traceback']`, not `exc_info`.** A live
  `exc_info` is formatted by the *application's* handler, from the exception
  objects themselves, and the chained `aiohttp` exception stringifies to the
  unredacted URL. The traceback is rendered and redacted before the record
  leaves this library. A handler reading `record.exc_info` finds nothing.
- **`ujson` is replaced by `orjson`**, and `requests` and `pytz` are gone.
  `ujson` was imported but **never declared as a dependency**, which is why the
  package was unimportable from a clean install. A custom `serialization`
  callable must return `str`, not `bytes`; one that does not is rejected at
  construction.
- **The dependency floors moved up**: `aiohttp>=3.14.3,<4`,
  `orjson>=3.12.0,<4`, `aioboto3>=15.5.0,<16`, `aiofiles>=25.1.0,<26`,
  `aioftp>=0.28.0,<1`, `asyncssh>=2.24.0,<3`. Each is a range with an approved
  floor and a major-version ceiling.
- **`requires-python` is now `>=3.10`** and declared, so an unsupported
  interpreter gets pip's clean message instead of a resolution cascade.
- **Packaging is declarative** (PEP 517 / PEP 621). `setup.py` and `setup.cfg`
  are gone. `download_url` is removed — it pointed at **a different project's**
  release tarball.

### Fixed

- Query-parameter redaction no longer depends on classifying a string as a URL,
  a predicate that failed open three separate times. Sensitive `name=value`
  pairs are masked wherever they appear — in the envelope's `url`, in
  `error['message']` and `error['cause']`, and in the log record.
- Request-tracer results are per-request rather than shared by the
  `TraceConfig`, so two concurrent calls no longer interleave their timings or
  annotate each other's results.
- FTP `upload` and SFTP `put` passed the **remote** path as the **local**
  source. In a mirrored-tree deployment the wrong file was transferred to the
  wrong destination while the envelope reported `ok=True`.
- A successful FTP deletion is reported as success. `stat` ran after every
  command, including the removing ones, so a completed deletion reported a
  failure that a retrying caller then re-attempted.
- SFTP reported the truth about the operation it completed. It returned `True`
  instead of an envelope, and never reached even that: a string-parse of an
  undocumented `asyncssh` repr raised `KeyError` before the transfer ran.
- The client TLS context is built for `Purpose.SERVER_AUTH`. It was built with
  `Purpose.CLIENT_AUTH` — the constructor a *server* uses — so client
  certificates had **never once connected**, and `verify_ssl=True` with a
  certificate negotiated against an entirely unauthenticated peer.
- No blocking filesystem I/O remains on any async path; a package-wide AST scan
  in the test suite keeps it that way.
- `protocol_info=None` no longer raises `AttributeError`, and an unknown
  protocol name no longer raises `KeyError` from a function documented to
  return a dict. `'http'`, `' HTTP '` and `'Http'` are the same protocol.
- `protocol='HTTPS'` now genuinely requires TLS. It previously mapped to the
  same class as `'HTTP'` with nothing distinguishing them, so a caller who
  explicitly asked for TLS and passed an `http://` URL got silent plaintext.
  The scheme is checked **after** any pre-processor runs and against the URL
  actually dispatched, so a pre-processor cannot downgrade the call.
- A caller's `protocol_info` dict is no longer written to. It previously came
  back carrying a live `RetryPolicy` object, and an SFTP directory `get` left
  `recurse` set on it for every later call.

### Removed

- `helpers/common/file_helper.py` and the dead `fetch_file` it existed for.
- `tests/coverage_output.py`, a script that crashed under branch coverage.
- The `Development Status :: 5 - Production/Stable` classifier — see below.

### Decisions recorded in this release

- **Resilience library: `pyfailsafe` is kept, with a named cost.** Its
  circuit-breaker behaviour was measured rather than assumed, and one fitness
  behaviour is provably absent: it hardcodes `time.monotonic()`
  (`circuit_breaker.py:139,142`) and `await asyncio.sleep(...)`
  (`failsafe.py:103`), so it offers no clock or sleep seam a test can move.
  Rather than swap or vendor, this library's own facade
  (`helpers/internal/circuit_breaker_helper.py`) owns the whole breaker state
  machine and the whole retry loop against an injected clock, and the
  dependency supplies exception classification and backoff computation only.
  The named cost is that residual — `retry_policy.py`, 136 of 539 lines, ~40
  non-trivial — and the revisit trigger is the first reach into `failsafe`
  internals or the first behaviour to regress on a new interpreter. The full
  record is `docs/decisions/0001-resilience-library.md`.
- **Documentation: the Sphinx scaffolding is retired.** `docs/` contained
  `make.bat`, `source/conf.py` and `source/Makefile` and **zero `.rst` files**,
  so there was no root document and Sphinx could not build; the Makefile also
  set `SOURCEDIR = source` from inside `docs/source/`, resolving to
  `docs/source/source`, and `make.bat` is Windows-only. There has never been a
  working build path. The three files are deleted and `sphinx` and
  `sphinx-rtd-theme` are dropped from the dev dependencies. **`README.md` is
  now this library's documentation**, with `docs/specs/` and `docs/decisions/`
  retained. The `release = '2.1'` version claim in `conf.py` — a third,
  independent version number — goes with them.
- **`Development Status :: 4 - Beta`, not `5 - Production/Stable`.** The
  inherited metadata claimed Production/Stable for a package that could not be
  imported from a clean install. Beta is the honest claim for a first release
  under this name: the API is settled and the test suite is thorough, but this
  rewrite has not yet run in anyone's production system — whatever field
  experience `asyncio-requests` accumulated was against code this release
  largely replaces — and that is precisely what the classifier is asked to
  report. It is worth revisiting once there is field experience to point at.

### Versioning policy

This project follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
**This section is the single statement of the policy**; `README.md`'s
"Versioning policy" section links here rather than restating it, so the two
cannot drift apart.

Given `MAJOR.MINOR.PATCH`, this project increments:

- **MAJOR** for a breaking change — and treats each of the following as
  breaking: removing or renaming a key of the response envelope; changing the
  type or meaning of one; removing or renaming a `protocol_info` key, or
  changing its default in a way that alters what goes over the wire; changing
  the value of an `error['code']`; removing a public function or changing its
  signature incompatibly; raising `requires-python`; and **tightening a
  security default**, which is breaking even though it is an improvement, since
  it can turn a working call into a failing one.
- **MINOR** for a backwards-compatible addition — a new protocol, a new
  optional `protocol_info` key, a new envelope key, a new `error['code']` value
  for a failure mode that previously reported a more general one.
- **PATCH** for a backwards-compatible fix.

Two things are explicitly **not** part of the public API and may change in any
release: anything under `asyncio_gateway.helpers.internal`, and the exact wording
of `error['message']`. Branch on `error['code']`, which is stable, and never on
message text.

[Unreleased]: https://github.com/ajyadav013/asyncio-gateway/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ajyadav013/asyncio-gateway/releases/tag/v1.0.0
