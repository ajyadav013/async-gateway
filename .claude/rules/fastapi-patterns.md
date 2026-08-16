---
paths:
  - "**/*.py"
---

# async-gateway library patterns

Stack-specific conventions for this repository's Python code. It complements the generic rules —
read `.claude/rules/code-organization.md`, `.claude/rules/design-patterns.md`, and
`.claude/rules/testing.md` first; this file makes them concrete for **async-gateway**.

> **The filename is wrong and is kept deliberately.** claude-kit generated this overlay from a
> FastAPI-service template and `CLAUDE.md` references it by path. Renaming it would break that
> reference, so the name stays `fastapi-patterns.md` until a story owns both files at once. There is
> no FastAPI in this project; the content below is what applies.

## What this project is

`async-gateway` is a **distributable Python library** — a single public coroutine,
`async_gateway.async_gateway.request()`, that dispatches a call over one of several protocols and
returns one uniform response envelope. It is **not a web service**: nothing here serves HTTP, there
is no application object, and there is no dev server to run.

- **Python ≥ 3.10** (`requires-python` in `pyproject.toml`), asyncio throughout.
- Transports: `aiohttp` (HTTP/HTTPS), `aioftp` (FTP), `asyncssh` (SFTP); `orjson` for JSON;
  `pyfailsafe` behind the circuit breaker.
- Tests: **pytest** + **pytest-asyncio** (`asyncio_mode = "auto"`), `pytest-randomly` (tests run in
  a random order every time — order dependence fails loudly, which is intended).
- Lint: **flake8** (config in `.flake8`, because flake8 cannot read `pyproject.toml`) with the
  `builtins`, `docstrings`, `import-order`, and `quotes` plugins. Types: **mypy**.
- **No autoformatter is configured** — this project has none at all. Match the surrounding file by
  hand; `flake8` is what judges the result.

Run the project's own commands for install, test, lint, types and build — see the **Commands**
section of `CLAUDE.md`, which is the single source of truth for them.

## Architecture (the call path, top to bottom)

```
async_gateway/async_gateway.py     request(): the one public entry point.
                                   Validates caller config, seeds the envelope,
                                   resolves the protocol, dispatches, and is the
                                   ONLY place an AsyncGatewayError becomes an
                                   ok=False envelope.
  -> logic/__init__.py             protocol_mapping: normalised name -> strategy class.
    -> logic/{http,ftp,sftp}_client.py
                                   One BaseRequestClass subclass per protocol.
                                   Fills the envelope it was handed; raises on failure.
      -> helpers/internal/base.py  BaseRequestClass: the strategy contract, plus
                                   validated_protocol_info() used at the boundary.
      -> helpers/internal/*        circuit breaker, request/response/filters helpers.
utils/envelope.py                  GatewayResponse TypedDict + new_envelope /
                                   finalise_ok / finalise_error. The only response shape.
utils/exceptions.py                AsyncGatewayError hierarchy; every class declares a
                                   wire-stable `code`.
utils/status_map.py                code -> status_code, and WARNING_CODES for log level.
utils/redaction.py                 the only place credential values are masked.
```

Four rules of thumb hold the design together, and each replaced a real defect:

- **One conversion point.** Only `request()` turns an `AsyncGatewayError` into an `ok=False`
  envelope. Everything that is *not* an `AsyncGatewayError` — a `KeyError`, a `TypeError` — must
  propagate, because it is this library's bug and reporting it as a failed network call is what hid
  most of an audit for the life of the package. **Never add a blanket `except Exception`.**
- **One envelope object.** `request()` builds it; the protocol class fills *that same object* and
  returns it. A protocol never constructs a response shape and never returns `True`.
- **Validate at the boundary, once.** `resolve_protocol()` and `validated_protocol_info()` run in
  `request()` before anything is dispatched. Layers below act on validated data and do not re-check
  it — two checks are how one of them comes to accept what the other rejects.
- **Redact at the seam, not at each consumer.** `utils/redaction.py` masks credential values; the
  normalised `redact_query_params` set is computed once in `request()` and handed down, so the
  envelope, the exception messages and the failure log cannot disagree about what a secret is.

## Adding a new protocol (the recipe)

To add `<PROTO>` (SOAP is the next one, and is deliberately absent from the registry until its
client module exists — an absent key is an unknown protocol, which `resolve_protocol()` rejects
with a `ConfigurationError` naming what *is* supported):

1. **Protocol class** — `async_gateway/logic/<proto>_client.py`. Subclass `BaseRequestClass`.
   Declare `REQUIRED_INFO_KEYS: ClassVar[frozenset[Text]]` for the `protocol_info` keys the
   protocol cannot run without (HTTP declares `{'request_type'}`; a protocol whose every key has a
   default inherits the empty default). Read config off `self.info` in `__init__`, never off the
   raw parameter. Implement `async def handle_request(self) -> GatewayResponse`.
2. **Registry entry** — add `'<PROTO>': <Proto>Request` to `protocol_mapping` in
   `async_gateway/logic/__init__.py`. The mapping is typed
   `Final[dict[str, type[BaseRequestClass]]]`, so a non-class entry is a mypy error rather than a
   `TypeError: 'NoneType' object is not callable` at the caller. If the protocol needs a URL-scheme
   allowlist the way HTTPS does, add it to `HTTP_FAMILY_SCHEMES` in `async_gateway/async_gateway.py`
   — that table, not the registry, is what makes `'HTTP'` and `'HTTPS'` differ while both map to
   `HttpRequest`.
3. **Envelope mapping** — fill `self.response` from the protocol's result and close it with
   `finalise_ok(self.response, status_code=..., started=self.start_time)`. The top-level key set is
   invariant across every protocol, so anything protocol-specific goes in `protocol_details`, never
   as a new top-level key. Write the response into the envelope **before** raising a status error,
   so an `ok=False` never costs the caller the body (invariant E11).
4. **Error mapping** — add a `ProtocolError` subclass in `utils/exceptions.py` with a new
   wire-stable `code`, and its default status in `STATUS_BY_CODE` in `utils/status_map.py`. If the
   failure is the remote side's answer rather than a transport fault, add the code to
   `WARNING_CODES` so it logs at `warning` and not `error`. Translate the transport library's own
   exceptions into the existing `TransportError` family (`ConnectError`, `DnsError`, `TlsError`,
   `GatewayTimeoutError`) — catch the *specific* exceptions, and chain with `from err` so
   `unwrap_cause` can find a non-empty message.
5. **Tests** — `tests/test_<proto>.py`, alongside the existing top-level test modules (`tests/`
   keeps only `helpers/` and `fixtures/` as subpackages). Cover: the registry
   resolves the name (and case/whitespace variants); a missing required `protocol_info` key raises
   `ConfigurationError` before dispatch; a success envelope; a remote-failure envelope that still
   carries the body; each transport failure mapping to its exception type; and that a credential in
   the URL or payload is masked in the returned envelope *and* in the log record.
6. **README section** — add the protocol to `README.md` alongside the existing HTTP / FTP / SOAP
   sections: its `protocol_info` keys, a sample call, and a sample response. The README is the
   library's user-facing documentation and a new protocol is not shipped until it is there.

## Conventions

- **Type everything.** Full annotations on every public function, and a Google-style or
  reST-style docstring (args, returns, raises) — `flake8-docstrings` enforces the presence of one.
  Note that `mypy async_gateway` is currently clean only because `ignore_errors = true` is still
  set in `pyproject.toml`; a later story removes it, so do not read a clean mypy run as proof your
  annotations are right.
- **Single quotes.** `flake8-quotes` flags double quotes (`Q000`). Existing code is single-quoted
  throughout; match it.
- **79 columns.** `max_doc_length = 79` in `.flake8`, and the default `E501` line limit applies to
  code.
- **Import order** is checked by `flake8-import-order`. Be aware that
  `application_import_names = async-gateway, tests` in `.flake8` is not a legal module name, so
  first-party `async_gateway` imports are currently classified as third-party — which is why they
  sit in the same block as `aiohttp` with no blank line between. Match what the file does; fixing
  the setting is a separate story's job.
- **Errors:** raise a typed `AsyncGatewayError` subclass. Never raise a bare `Exception`, never
  return an error dict, and never put an exception object into `response['text']` — that field is
  the decoded body and is typed `str`.
- **Logging:** the module logger, with structured `extra={}`. Every scalar in `extra` goes through
  `redact_value` (or `redact_text` for prose containing URLs) before it is logged. Do not use
  `exc_info=True`: a live `exc_info` is rendered by whichever handler the *application* installed,
  from exception objects that stringify to the unredacted URL, and there is no way to mask that
  after the fact.
- **Constrained values are constants, not literals** — the code strings on the exception classes
  and the tables in `utils/status_map.py` are the pattern.

## Which tests to run for a change

`pytest` runs the whole suite in under a second, so the default is to run all of it. Scope only
when iterating:

| Changed | Run |
|---|---|
| a protocol client | `pytest tests/test_<proto>.py` while iterating, then the full suite |
| the entry point, the registry, or the scheme table | `pytest tests/test_entrypoint.py` then the full suite |
| the envelope, exceptions, status map, or redaction | the full suite — every protocol depends on them |
| packaging metadata | `pytest tests/test_packaging.py` |

Coverage is ratcheted: `fail_under` in `[tool.coverage.report]` may only ever be raised. A change
that lowers total coverage fails the run.

## Pre-removal search recipe

Before deleting a symbol, sweep every surface:

```bash
SYM=TheSymbolOrName           # e.g. FTPRequest  or  'SFTP'  or  RESPONSE_TOO_LARGE

grep -rn "$SYM" async_gateway/                  # entry point, logic, helpers, utils
grep -rn "$SYM" tests/                          # including fixtures/ and conftest.py
grep -rn "$SYM" README.md docs/                 # user-facing docs and specs
grep -rn "patch(.*$SYM\|monkeypatch.*$SYM" tests/   # the most-missed: patch targets
```

A `unittest.mock.patch('async_gateway.logic.http_client.HttpRequest')` target that no longer
resolves **fails silently** — the patch hits nothing and the test passes against a non-existent
symbol. Search tests explicitly, then remove one symbol at a time and run the suite.

Two symbol classes are **wire-stable** and are a breaking change to remove or rename, not a
refactor: the `code` string on any `AsyncGatewayError` subclass, and any top-level key of
`GatewayResponse`. Consumers branch on both.
