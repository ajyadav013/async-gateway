# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [1.0.0] — unreleased

The first release of this package, and a near-total rewrite of the code it
inherited. Read the two notes below before the change list: they are what make
a release numbered *lower* than its predecessor safe.

### About the version going down, from 2.7.3 to 1.0.0

**This package has never been published.** `pip install async-gateway` returns
404 from PyPI: there is no `2.x` on any index, no release history, and
therefore **zero installed users**. The `2.7.3` in the packaging metadata was
inherited from the fork this repository was squashed from; the `2.x` lineage
itself is not in this repository's history at all.

That is the whole of why the reset is safe, and the safety is conditional on
it. Nobody can be broken by a version moving backwards when nobody could ever
have installed the version it moved back from. There is no resolver to
confuse, no pin to invalidate, no `>=2.0` constraint in anyone's requirements
file. Had a single `2.x` been uploaded, this release would have had to go
*forward* instead, and the breaking changes below would have needed a
deprecation path rather than a clean statement.

`1.0.0` is therefore an honest first release: it says "this is version one of
a library you have not used before," which is true, where `2.7.4` would have
implied a lineage of published releases that does not exist.

### About the breaking changes

They are listed as breaking, plainly and in full, because that is what they
are — measured against the previous *code*, not against a previous *release*.
With no consumers, none of them breaks anyone today. They are recorded so that
someone comparing this code against the old README, an internal fork, or a
vendored copy can see exactly what moved, rather than discovering it at
runtime.

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
  `async_gateway/` without touching `CHANGELOG.md`.
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
  `pyproject.toml` and `mypy async_gateway` exits 0 on the 24 real errors it
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
  inherited metadata claimed Production/Stable for a package that had never
  been published and could not be imported from a clean install. Beta is the
  honest claim for a first release: the API is settled and the test suite is
  thorough, but no version of this library has yet run in anyone's production
  system, and that is precisely what the classifier is asked to report. It is
  worth revisiting once there is field experience to point at.

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
release: anything under `async_gateway.helpers.internal`, and the exact wording
of `error['message']`. Branch on `error['code']`, which is stable, and never on
message text.

[Unreleased]: https://github.com/ajyadav013/async-gateway/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ajyadav013/async-gateway/releases/tag/v1.0.0
