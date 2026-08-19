# asyncio-gateway

One `await` for HTTP, HTTPS, SOAP, FTP and SFTP. Every protocol returns the
**same response envelope**, so a consuming service writes one success check and
one error path instead of five.

```python
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='https://api.example.com/v1/items',
    protocol='HTTPS',
    protocol_info={'request_type': 'get'},
)
if result['ok']:
    print(result['json'])
else:
    print(result['error']['code'], result['error']['message'])
```

`asyncio-gateway` is a **library**, not a service. There is nothing to start, no
endpoint it serves and no configuration file it reads: you import `request()`
and call it.

---

## Contents

- [Install](#install)
- [Migrating from `asyncio-requests`](#migrating-from-asyncio-requests)
- [Quickstart per protocol](#quickstart-per-protocol)
- [Public API reference](#public-api-reference)
- [The response envelope](#the-response-envelope)
- [Error handling](#error-handling)
- [Retry, timeout and circuit-breaker behaviour](#retry-timeout-and-circuit-breaker-behaviour)
- [Transport security](#transport-security)
- [You own URL validation](#you-own-url-validation)
- [Local files: downloads, uploads and overwrite](#local-files-downloads-uploads-and-overwrite)
- [Redaction and the payload echo](#redaction-and-the-payload-echo)
- [Logging](#logging)
- [Supported Python versions](#supported-python-versions)
- [Versioning policy](#versioning-policy)
- [Contributing](#contributing)
- [Changelog](#changelog)
- [Licence and attribution](#licence-and-attribution)

---

## Install

```text
pip install asyncio-gateway
```

From a checkout, for development:

```text
pip install -e '.[dev]'
```

Runtime dependencies are declared as ranges with a major-version ceiling, so
installing this library does not pin your project to one patch release:
`aiohttp>=3.14.3,<4` · `orjson>=3.12.0,<4` · `aioboto3>=15.5.0,<16` ·
`aiofiles>=25.1.0,<26` · `aioftp>=0.28.0,<1` · `asyncssh>=2.24.0,<3` ·
`pyfailsafe==0.6.0`.

`pyfailsafe` is the one exact pin: `0.6.0` is the latest release of a dormant
project, and the decision to keep, replace or vendor it is recorded under
`docs/decisions/`.

JSON is encoded with **orjson**. Note that `orjson.dumps` returns `bytes`; the
library wraps it so aiohttp's `str`-returning contract holds. If you supply your
own `serialization` callable it must return `str`.

---

## Migrating from `asyncio-requests`

**This package is the continuation of
[`asyncio-requests`](https://pypi.org/project/asyncio-requests/), under a new
name.** That distribution is retired at `2.7.3` and will receive no further
releases; development continues here. If you install `asyncio-requests` today,
this is where it went.

The rename is why the version resets from `2.7.3` to `1.0.0`: `asyncio-gateway`
is a new distribution name with no release history, so the reset cannot move
any installed package backwards or invalidate any pin. It also means `pip
install --upgrade asyncio-requests` will **not** find this release — migrating
is a deliberate step, not something a resolver does to you.

**Why `asyncio-gateway` and not `async-gateway`.** `async-gateway` was the
intended name and PyPI rejected it: it compares
[PEP 503](https://peps.python.org/pep-0503/) *normalised* names
(`re.sub(r'[-_.]+', '-', name).lower()`), under which `async-gateway`,
`async_gateway` and the already-published `asyncgateway` are all one name.
`asyncio-gateway` was checked free in both spellings before it was adopted.
The full account is in [CHANGELOG.md](CHANGELOG.md).

To migrate:

1. Replace `asyncio-requests` with `asyncio-gateway` in your dependencies.
2. Change the import path — `asyncio_requests` becomes `asyncio_gateway`. The
   entry point keeps its name:

```text
# before
from asyncio_requests.asyncio_request import request
# after
from asyncio_gateway.asyncio_gateway import request
```

3. Work through the breaking changes in [CHANGELOG.md](CHANGELOG.md): the
   single response envelope, the removal of `api_response`, `tat` renamed to
   `latency`, the FTP `verify_ssl` default flip, SFTP host-key verification on
   by default, and the `logic/*` module renames.

**Before you defer this:** `asyncio-requests <= 2.7.3` ships with SSH host-key
verification disabled on SFTP, an FTP path that raises on every call, and a
SOAP module that is an empty file. All three are fixed here. The details, the
impact of each, and what to do if you cannot migrate yet are in the
[security advisory](CHANGELOG.md#security-advisory--asyncio-requests--273).

---

## Quickstart per protocol

Every example below is executed against a live server by this project's own
test suite (`tests/test_docs.py`), so an example that stops working fails CI.

<a id="runnable-example-scripts"></a>
**Complete runnable scripts live in the repository, not in the package.** The
five end-to-end programs under
[`examples/`](https://github.com/ajyadav013/asyncio-gateway/tree/master/examples) —
`http_example.py`, `ftp_example.py`, `sftp_example.py`, `soap_example.py` and
`error_handling_example.py` — are deliberately **not** shipped in the wheel or
the sdist, so `pip install asyncio-gateway` does not place them on your disk.
That is a decision rather than an oversight: a second copy of the API's
documentation inside every install is a copy that drifts against this README.
Read them on GitHub or in a clone; the snippets below are self-contained and
are what the test suite executes.

### HTTP / HTTPS

```python
import aiohttp
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='https://api.example.com/v1/items',
    data={'name': 'widget', 'quantity': 3},
    protocol='HTTPS',
    protocol_info={
        'request_type': 'post',
        'timeout': 10,
        'headers': {
            'Content-Type': 'application/json',
            'Authorization': aiohttp.encode_basic_auth(
                'svc-orders', 'not-a-real-password'),
        },
    },
)
assert result['ok'] is True
assert result['status_code'] == 201
assert result['json'] == {'id': 'w-1'}
```

`protocol='HTTPS'` refuses to dispatch a plain `http://` URL — that is the whole
difference between it and `protocol='HTTP'`, which accepts either scheme. A
schemeless URL under `'HTTPS'` is upgraded to `https://`, because there is
exactly one scheme that can satisfy the protocol the caller named.

<a id="http-basic-auth"></a>
**HTTP credentials go in an `Authorization` header, not `auth=`.** aiohttp
deprecated `BasicAuth` in 3.14: constructing one emits a `DeprecationWarning`
and it is removed in aiohttp 4.0. This library pins `aiohttp<4`, so the class
still works today — but the warning is raised in *your* code, at the line that
constructs it, which is why the example above does not use it.
`aiohttp.encode_basic_auth(user, password)` is the replacement aiohttp's own
warning names; it returns the ready-made `'Basic <base64>'` header value. The
`Authorization` header is redacted from logs and from the response envelope
just as a `BasicAuth` object was, so nothing is given up by moving to it.

### FTP

FTP speaks TLS by default (`verify_ssl` defaults to `True`) and never silently
downgrades to plaintext.

```python
from types import SimpleNamespace
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='ftp.example.com',
    auth=SimpleNamespace(login='deploy', password='not-a-real-password'),
    protocol='FTP',
    protocol_info={
        'port': 21,
        'command': 'download',
        'server_path': '/exports/report.csv',
        'client_path': LOCAL_DOWNLOAD_PATH,
        'verify_ssl': True,
        'timeout': 30,
    },
)
assert result['ok'] is True
assert result['protocol_details']['command'] == 'download'
```

`url` is a **bare host name** for FTP and SFTP, not a URL with a scheme; the
port comes from `protocol_info['port']`.

`protocol_info['port']` must be an **integer in `0..65535`**, on every
protocol. Anything else — a string, a float, a `bool`, a negative number, or an
unhashable value such as a list — is refused with a `ConfigurationError`
(`code='CONFIG'`, status `400`) before the call is dispatched. Omit the key to
take the port from the URL, or from the protocol family's default.

**FTP accepts any object carrying string `.login` and `.password`
attributes.** The example uses `types.SimpleNamespace` from the standard
library. SFTP has a protocol-specific `SFTPAuth` model because SSH accepts a
password, an explicit client key, or both. Legacy SFTP auth objects remain
supported.

> **⚠️ FTP names its paths differently from SFTP.** FTP uses
> **`server_path`/`client_path`**; SFTP uses **`remote_path`/`local_path`**.
> They mean the same two things — the server side and this machine's side — and
> the difference is deliberate: each pair mirrors the vocabulary of the library
> underneath (`aioftp` for FTP, `asyncssh` for SFTP). **The pairs are not
> interchangeable**, but mixing them fails loudly rather than silently: an FTP
> call given `remote_path` is refused with
> a `DeprecationWarning` for the unknown `remote_path`, followed by
> `error['code'] == 'CONFIG'` and the message
> `protocol_info['server_path'] must be a non-empty string naming the path on
> the server, got None` — the unexpected key is ignored and the missing one is
> named. Check the table for the protocol you are actually calling — the two
> tables are [FTP](#protocol_info-ftp) and [SFTP](#protocol_info-sftp).

`server_path` is always the path *on the server* and `client_path` the path *on
this machine*, whichever direction the transfer goes: a `download` reads
`server_path` and writes `client_path`, an `upload` reads `client_path` and
writes `server_path`.

### SFTP

SFTP verifies the server's SSH host key. By default that is asyncssh's own
`~/.ssh/known_hosts` resolution; here the trusted set is pinned explicitly.

```python
from asyncio_gateway import SFTPAuth
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='sftp.example.com',
    auth=SFTPAuth(username='deploy', password='not-a-real-password'),
    protocol='SFTP',
    protocol_info={
        'port': 22,
        'mode': 'get',
        'remote_path': '/exports/report.csv',
        'local_path': LOCAL_DOWNLOAD_PATH,
        'known_hosts': KNOWN_HOSTS_PATH,
    },
)
assert result['ok'] is True
assert result['protocol_details']['mode'] == 'get'
assert result['protocol_details']['file_stats']['type'] == 'file'
```

`remote_path` is always the server side and `local_path` this machine's side: a
`'get'` reads `remote_path`, a `'put'` reads `local_path`. See
[Transport security](#transport-security) for pinning, the named bypass and
key-based authentication.

### SOAP

```python
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='https://api.example.com/soap',
    data='<GetRate xmlns="urn:rates"><Pair>EURUSD</Pair></GetRate>',
    protocol='SOAP',
    protocol_info={
        'soap_version': '1.1',
        'soap_action': 'urn:rates#GetRate',
    },
)
assert result['ok'] is True

# `soap_body` is a raw ElementTree Element, not a mapping.
body = result['protocol_details']['soap_body']
assert body.tag == '{urn:rates}Rate'
assert body.findtext('{urn:rates}Value') == '1.09'
```

**Three things to know before you write any SOAP code.**

1. **The request body must be a hand-built XML string or an
   `xml.etree.ElementTree.Element`.** There is **no dict-to-XML mapping**:
   choosing element names, ordering and namespaces for a mapping needs the
   service schema, which needs the WSDL, and this library does not read WSDL. A
   `dict` body raises `ConfigurationError`.
2. **`protocol_details['soap_body']` is a raw `Element` (or `None`), not a
   parsed mapping.** It is the *first element child* of `<Body>`. Navigate it
   with ElementTree's own API — one line, as above:
   `body.findtext('{urn:rates}Value')`.
3. **MTOM / attachments (`multipart/related`) are not supported** and raise
   `ConfigurationError` rather than being mis-parsed. Nothing is written to disk
   for a refused multipart response.

**WSDL introspection and code generation are not supported**, deliberately and
permanently: that is the scope this library declines.

Building the body with ElementTree instead of a string:

```python
from xml.etree.ElementTree import Element, SubElement
from asyncio_gateway.asyncio_gateway import request

call = Element('{urn:rates}GetRate')
SubElement(call, '{urn:rates}Pair').text = 'EURUSD'

result = await request(
    url='https://api.example.com/soap',
    data=call,
    protocol='SOAP',
    protocol_info={'soap_version': '1.2', 'soap_action': 'urn:rates#GetRate'},
)
assert result['ok'] is True
```

**Version differences on the wire**, which this library handles for you:

| | SOAP 1.1 | SOAP 1.2 |
|---|---|---|
| Envelope namespace | `http://schemas.xmlsoap.org/soap/envelope/` | `http://www.w3.org/2003/05/soap-envelope` |
| `Content-Type` | `text/xml; charset=utf-8` | `application/soap+xml; charset=utf-8` |
| Action carried in | a `SOAPAction` header, always present (`""` when none) | a `Content-Type` parameter, `;action="…"` |

A body that is **already a complete `<Envelope>`** is sent unwrapped rather than
nested inside a second one.

**Faults.** A SOAP Fault is `ok=False` with `error['code'] == 'SOAP_FAULT'` — at
*any* transport status, HTTP 200 included, which is the case a status-only check
misses. `status_code` carries the real HTTP status; nothing is synthesised. The
structured fault is in `protocol_details['soap_fault']`, flattened into one
shape for both versions:

```python
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='https://api.example.com/soap',
    data='<GetRate xmlns="urn:rates"><Pair>XXXYYY</Pair></GetRate>',
    protocol='SOAP',
    protocol_info={'soap_version': '1.1'},
)
assert result['ok'] is False
assert result['error']['code'] == 'SOAP_FAULT'
assert result['status_code'] == 500

fault = result['protocol_details']['soap_fault']
assert fault['code'] == 'soap:Client'
assert fault['reason'] == 'Unknown currency pair'
assert fault['subcodes'] == []          # 1.2 only; always [] for 1.1
assert fault['actor'] is None
```

`fault['detail']` is the `<detail>` element **re-serialised as XML text**, or
None — a fault detail routinely carries application XML whose schema this
library knows nothing about, and a string preserves it exactly.

It is also None when the detail **nests deeper than 64 levels**, which is
refused rather than serialised. Re-serialising recurses once per level, and a
1000-level detail is only 7 KB on the wire — far under `max_response_bytes`, so
the byte cap cannot see it — which would exhaust the interpreter stack and get
the server's Fault reported as a `STACK_EXHAUSTED` 502 rather than the
`SOAP_FAULT` it is. The rest of the fault is unaffected: `code`, `reason`,
`subcodes` and `actor` are read without recursing, so a too-deep detail costs
you the detail and never the fault. A warning naming the limit is logged when
it happens.

An XML response whose **prolog declares a `DOCTYPE`** is refused before parsing
with `error['code'] == 'XML_UNSAFE'`: a DOCTYPE is the entry condition for
entity-expansion attacks and a SOAP envelope never legitimately carries one. The
check is prolog-scoped, so a `<detail>` whose *content* contains the word
DOCTYPE parses normally.

---

## Public API reference

### `request()`

```python
async def request(
    url: str,
    data: Optional[Union[Dict, str]] = None,
    auth: object = None,
    protocol: str = '',
    protocol_info: Dict = None,
    pre_processor_config: Dict = None,
    post_processor_config: Dict = None,
    **kwargs,
) -> GatewayResponse: ...
```

| Argument | Meaning |
|---|---|
| `url` | An absolute URL for HTTP/HTTPS/SOAP; a **bare host name** for FTP/SFTP. |
| `data` | The request payload. For SOAP it is the XML body (`str` or `Element`). Defaults to `{}`. |
| `auth` | **Required for FTP and SFTP.** FTP accepts string `.login` and `.password` attributes. SFTP accepts `SFTPAuth(username=..., password=..., client_keys=..., key_passphrase=..., use_ssh_agent=False)` and retains legacy auth objects. **Optional for HTTP/HTTPS/SOAP**, where `None` sends no credentials; put credentials in an `Authorization` header instead (see [HTTP credentials](#http-basic-auth)). |
| `protocol` | One of `'HTTP'`, `'HTTPS'`, `'FTP'`, `'SFTP'`, `'SOAP'`. Matched with surrounding whitespace stripped and without regard to case, so `'http'`, `' HTTP '` and `'Http'` are one protocol. |
| `protocol_info` | Per-protocol configuration; see the tables below. `None` is a valid call for every protocol that requires no key. |
| `pre_processor_config` | `{'function': async_callable, 'params': {...}}`. Awaited before dispatch with `response=<envelope>` plus `params`; its return value lands in `pre_processor_response`. See [Processor hooks](#processor-hooks). |
| `post_processor_config` | The same shape, awaited after the call; its return value lands in `post_processor_response`. |
| `**kwargs` | No additional top-level names are accepted. A misspelling raises `ConfigurationError` before processors or transport code. Put transport options in `protocol_info` and callback arguments in a processor config's `params`. |

### Processor hooks

Both configs are validated **before either runs** — so a typo in your
post-processor config refuses the call up front, rather than after the request
has already gone out and cannot be un-sent.

A malformed *configuration* raises `ConfigurationError` (`CONFIG`/400): a
config that is not a mapping, a missing or non-callable `'function'`, a
`'params'` that is not a mapping of `str` keys, or a `'params'` naming
`'response'` — which this library passes itself, so supplying it too would give
your callable two values for one argument.

A *valid* config whose callable then fails raises `ProcessorError`
(`PROCESSOR`/500), with the original chained as `__cause__`. That covers your
function raising, refusing the `response` keyword, returning something that
cannot be awaited, removing a required key, or changing pre-dispatch `url` or
`protocol`. The two
errors are deliberately distinct: the first says the hook was configured
wrongly, the second says the hook that was configured is itself broken.

Your callable is handed the **live** envelope and may change non-routing values
such as `payload`, `headers`, cookies and caller metadata. A pre-processor may
not rewrite `url` or `protocol`: the validated arguments to `request()` are the
single authority for dispatch. URL edits were previously accepted and silently
discarded, which made a callback appear to retarget a request that still went
to the original destination. Either edit now raises `ProcessorError` before a
protocol client is constructed, and the error names fields without echoing URL
values. A **post-processor** may rewrite report fields, including `url` and
`protocol`, because dispatch has already completed. Both hook types must
preserve the required envelope key set.

`protocol_info` also has an accepted key set per protocol, matching the tables
below. An unknown key resembling a security control (for example
`verify_sll`) raises `ConfigurationError` immediately. Other unknown keys emit
`DeprecationWarning` during the 1.x compatibility window and will become errors
in 2.0. The warning and error list the accepted keys.

A post-processor that raises therefore forfeits the envelope, response body
included. That is the cost of the guarantee that nothing but an
`AsyncGatewayError` escapes `request()`: an `ok=False` envelope would dress a
bug in your cleanup function up as a failed request and overwrite the
successful result you were about to read. A callback that must not cost you the
response handles its own failures. A callback that raises an
`AsyncGatewayError` itself is passed through untouched.

### `protocol_info` — HTTP and HTTPS

`request_type` is the only **required** key. There is no default verb: a GET
assumed for a caller who meant DELETE is worse than a rejected call.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `request_type` | `str` | **required** | The HTTP verb. See the verb table below. |
| `timeout` | positive number | `15` | Seconds for the **whole exchange**, redirect chain included. |
| `headers` | `dict` | `{}` | Request headers. Also selects the body encoding — see the media-type table. |
| `cookies` | mapping | `None` | Request cookies. |
| `verify_ssl` | `bool` | `True` | TLS verification. |
| `certificate` | `(cert path, key path)` | `None` | A client certificate pair for mutual TLS. |
| `session` | `aiohttp.ClientSession` | `None` | Your own session, reused and never closed by this library. |
| `serialization` | callable returning `str` | orjson | JSON encoder. Cannot be combined with `session`. |
| `trace_config` | list of `aiohttp.TraceConfig` | a built-in tracer | Request tracers. Cannot be combined with `session`. |
| `max_response_bytes` | positive `int` | `67108864` (64 MiB) | Ceiling on the response body. No sentinel disables it. |
| `allow_redirects` | `bool` | `True` | Whether to follow redirects at all. |
| `max_redirects` | non-negative `int` | `10` | How many hops to follow. |
| `allowed_schemes` | collection of `str` | `{'http', 'https'}` | Schemes a redirect hop may target. Enforced **per hop**. |
| `cross_origin_headers` | collection of `str` | `()` | Extra header names allowed to survive a cross-origin redirect. Cannot name a credential header. See [Redirects](#redirects). |
| `http_file_upload_config` | `dict` | `{}` | Upload a local file; see [Local files](#local-files-downloads-uploads-and-overwrite). |
| `http_file_download_config` | `dict` | absent | Stream the response to disk; see [Local files](#local-files-downloads-uploads-and-overwrite). |
| `circuit_breaker_config` | `dict` | `{}` | Retry and breaker settings; see [Retry](#retry-timeout-and-circuit-breaker-behaviour). |
| `redact_query_params` | list of `str` | `()` | Extra query-parameter names to mask. Added to the built-in set, never replacing it. |
| `port` | `int` | from the URL | Overrides the port used as the circuit-breaker key. |

**HTTP verbs.** `request_type` must name one of these seven, matched
case-insensitively with surrounding whitespace stripped. Anything else — a typo,
or `'close'`, which used to resolve to `ClientSession.close` — raises
`UnsupportedVerbError` before a socket is opened:

| Verb |
|---|
| `delete` |
| `get` |
| `head` |
| `options` |
| `patch` |
| `post` |
| `put` |

An `http_file_upload_config` combined with `get` is refused: a GET has no body
to upload a file in.

**How the request body is encoded**, chosen from the `Content-Type` you set in
`headers`:

| `Content-Type` | Encoding |
|---|---|
| `application/json` (and `+json` suffixes) | JSON |
| `application/x-www-form-urlencoded` | form |
| `text/xml`, `application/xml`, `application/soap+xml` | raw body, byte-for-byte |
| absent or anything else, payload is `str`/`bytes` | raw body |
| absent or anything else, any other payload | JSON |

A parameter on the header — `application/json; charset=utf-8` — selects the same
encoding as the bare media type.

### `protocol_info` — FTP

| Key | Type | Default | Meaning |
|---|---|---|---|
| `command` | `str` | **required at dispatch** | The operation. See the command table below. |
| `server_path` | `str` | `None` | The path on the server. |
| `client_path` | `str` | `None` | The path on this machine. Omit for a command that touches no local file. |
| `port` | `int` | `21` | Server port. |
| `verify_ssl` | `bool` | `True` | FTPS. `False` opens the session in **plaintext** and logs a warning. |
| `certificate` | `(cert path, key path)` | `None` | A client certificate pair. |
| `overwrite` | `bool` | `False` | Whether a download may replace an existing local file. |
| `timeout` | number | `15` | Bounds the connect, and bounds **each** socket read and write. |
| `circuit_breaker_config` | `dict` | `{}` | Retry and breaker settings. |
| `max_response_bytes` | positive `int` | `67108864` (64 MiB) | Ceiling on the total bytes a download may write locally, across **all** files of a recursive transfer. No sentinel disables it. |
| `redact_query_params` | list of `str` | `()` | Extra sensitive parameter names. |

**FTP commands** — `command` must name one of these five:

| Command |
|---|
| `download` |
| `upload` |
| `remove` |
| `remove_file` |
| `remove_directory` |

`timeout` bounds the connect and each individual socket operation, **not the
transfer as a whole**: a large file legitimately takes many operations, and a
server answering just inside the window can hold a transfer open. What it
removes is the hang — a socket that goes silent forever.

`protocol_details` carries `command`, `server_path`, `client_path` and
`file_stats` — the last is `None` after a command that removed the path.

### `protocol_info` — SFTP

| Key | Type | Default | Meaning |
|---|---|---|---|
| `mode` | `str` | **required at dispatch** | The operation. See the mode table below. |
| `remote_path` | `str` | `None` | The path on the server. |
| `local_path` | `str` | `None` | The path on this machine. Omit for an operation naming only a remote path. |
| `port` | `int` | `22` | Server port. |
| `known_hosts` | anything asyncssh accepts | *omitted* | Trusted host keys. Omitting it takes asyncssh's `~/.ssh/known_hosts` resolution. |
| `host_key` | one key | `None` | Pin exactly one trusted server key. |
| `insecure_skip_host_key_check` | `bool` | `False` | Disable host-key verification. Only literal `True` reaches it. |
| `client_keys` | anything asyncssh accepts | `None` | Legacy location for explicit client identities. Prefer `SFTPAuth.client_keys`; do not set both. |
| `additional_arguments` | `dict` | `{}` | Forwarded to the asyncssh operation. Never mutated. |
| `overwrite` | `bool` | `False` | Whether a download may replace an existing local file. |
| `timeout` | number | `15` | Bounds the connect and the login. |
| `circuit_breaker_config` | `dict` | `{}` | Retry and breaker settings. |
| `max_response_bytes` | positive `int` | `67108864` (64 MiB) | Ceiling on the total bytes a download may write locally, across **all** files of a recursive transfer. No sentinel disables it. |
| `redact_query_params` | list of `str` | `()` | Extra sensitive parameter names. |

**SFTP modes** — `mode` must name one of these three:

| Mode |
|---|
| `get` |
| `put` |
| `remove` |

`timeout` bounds the connect and the login; asyncssh offers no deadline for the
transfer itself, so a large file is not capped.

`protocol_details` carries `mode`, `remote_path`, `local_path`, `file_stats` and
`files`. `files` is `None` for a target that is not a directory and `[]` for one
that is empty — an empty directory and a single file stay distinguishable.
`file_stats['type']` is one of `'file'`, `'directory'`, `'symlink'`,
`'special'` or `'unknown'`.

### `protocol_info` — SOAP

SOAP reuses the HTTP transport, so every HTTP key above applies except four:
`request_type` (always POST), the two file-transfer configs (MTOM is out of
scope, so a SOAP call writes no local file), and `serialization` (the request
body is an XML envelope sent byte for byte, never a JSON document, so there is
nothing for a JSON encoder to encode). All four are **refused** if you pass
them, never silently ignored. That list is asserted against the code rather
than only stated here: a key `HttpRequest` reads and `SoapRequest` does not is
a test failure, in both directions. In addition:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `soap_version` | `'1.1'` or `'1.2'` | `'1.1'` | Any other value raises `ConfigurationError`. |
| `soap_action` | `str` | `None` | The action. A quote, CR or LF is refused as header injection. |
| `soap_headers` | `Element` | `None` | An element placed inside `<Header>`. A `str` is refused. |

`protocol_details` carries `soap_version`, `soap_body` and `soap_fault`.

### File-transfer utilities

These are exported from `asyncio_gateway.utils.http_file_config`, and that is the
only place they live:

```python
from asyncio_gateway.utils.http_file_config import (
    delete_local_file_path,
    download_file_from_s3,
    download_file_from_url,
)
```

```python
async def download_file_from_url(
    file_download_path: str,
    local_filepath: str,
    request_type: str = 'get',
    headers=None,
    timeout: Optional[float] = None,
    max_response_bytes: int = 67108864,
    chunk_size: int = 65536,
    overwrite: bool = False,
    **kwargs,
): ...


async def download_file_from_s3(
    *,
    bucket_name: str,
    s3_filepath: str,
    local_filepath: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: Optional[str] = None,
    **kwargs,
) -> None: ...


async def delete_local_file_path(local_filepath: str, **kwargs) -> None: ...
```

`download_file_from_s3` is **keyword-only**. A positional call is a `TypeError`
at the call site rather than a bucket name silently written to a local path.
Credentials that cannot be resolved raise `ConfigurationError`.

`download_file_from_url` refuses **every** non-success status, not only 403: a
404 body, a 500 stack trace or an HTML login page is never written to disk as
though it were the requested file. On refusal no file is created at all.

`delete_local_file_path` is **idempotent** — a path that is already gone is a
success, so it is safe to call from a `finally` or as a post-processor.

They compose with `request()` through the processor hooks:

```python
from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.utils.http_file_config import (
    delete_local_file_path,
    download_file_from_url,
)


async def fetch_the_attachment(response, **params):
    """Download the file this call is about to upload."""
    await download_file_from_url(**params)
    return 'downloaded'


async def clean_up(response, **params):
    """Remove the local copy once the upload has completed."""
    await delete_local_file_path(**params)
    return 'cleaned'


result = await request(
    url='https://api.example.com/v1/attachments',
    protocol='HTTPS',
    protocol_info={
        'request_type': 'post',
        'http_file_upload_config': {
            'local_filepath': LOCAL_UPLOAD_PATH,
            'file_key': 'attachment',
        },
    },
    pre_processor_config={
        'function': fetch_the_attachment,
        'params': {
            'file_download_path': FILE_SOURCE_URL,
            'local_filepath': LOCAL_UPLOAD_PATH,
            'overwrite': True,
        },
    },
    post_processor_config={
        'function': clean_up,
        'params': {'local_filepath': LOCAL_UPLOAD_PATH},
    },
)
assert result['ok'] is True
assert result['pre_processor_response'] == 'downloaded'
assert result['post_processor_response'] == 'cleaned'
```

### Request tracing

```python
from asyncio_gateway.utils.request_tracer import request_tracer

tracer = request_tracer()
```

Trace results are per **request**, not per tracer: one tracer shared across
concurrent calls yields one result mapping per call, and no call can see
another's events. The results land in the envelope's `request_tracer`. Supply
your own tracers through `protocol_info['trace_config']`, or attach them to your
own `ClientSession` — but not both, since `trace_configs` exists only on the
session constructor.

---

## The response envelope

`request()` returns a `GatewayResponse` — a `TypedDict` with **the same key set
for every protocol, on both the success and the failure path**. Import it for
type checking:

```python
from asyncio_gateway.utils.envelope import GatewayError, GatewayResponse
```

| Key | Type | Meaning |
|---|---|---|
| `ok` | `bool` | **The success predicate, and the only correct check.** |
| `status_code` | `int` | Always populated. See the mapping below. |
| `protocol` | `str` | The protocol the call was dispatched on, normalised. |
| `url` | `str` | The URL actually dispatched to, redacted. |
| `request_time` | `str` | ISO-8601, UTC, with an explicit offset. |
| `latency` | `float` | Seconds, measured with a monotonic clock. Never negative. |
| `payload` | `Any` | The request payload, key-name-redacted. See [the payload echo](#redaction-and-the-payload-echo). |
| `text` | `str` | The decoded response body; `''` when not applicable. |
| `json` | `dict`, `list` or `None` | The parsed body, or None when the response was not announced as JSON. |
| `headers` | `dict[str, str]` | Response headers, credential values redacted; `{}` for protocols that have none. |
| `cookies` | `dict[str, str]` | Response cookies, **every** value redacted; `{}` for protocols that have none. |
| `error` | `GatewayError` or `None` | The failure. None **exactly when** `ok` is True. |
| `protocol_details` | `dict[str, Any]` | Per-protocol extras, so the top-level key set stays invariant. |
| `request_tracer` | `list[MutableMapping[str, Any]]` | Per-request trace results; `[]` when tracing is off. Mapping-like, and read as one — for a tracer built by `request_tracer()` the entry is a live `ResultsCollector` view rather than a plain `dict`, so that concurrent calls sharing a tracer do not share results. |
| `pre_processor_response` | `Any` | What your pre-processor returned. |
| `post_processor_response` | `Any` | What your post-processor returned. |

`error`, when present, is a `GatewayError`:

| Key | Type | Meaning |
|---|---|---|
| `type` | `str` | The exception class name, e.g. `'GatewayTimeoutError'`. |
| `code` | `str` | The **wire-stable** machine-readable code, e.g. `'TIMEOUT'`. |
| `message` | `str` | Human-readable, and **never empty** when `ok` is False. |
| `cause` | `str` or `None` | The deepest cause's type and message, or None when the chain adds nothing. |

**Check `result['ok']`.** It is False for every failure, and it is the only
field that is. There is no `api_response` key: it was removed rather than
re-shaped, so old code doing `if result['api_response']:` fails loudly with a
`KeyError` instead of silently reading a truthy value.

**A failure never costs you the response.** On a remote-status failure — an HTTP
4xx/5xx, a SOAP Fault, an FTP or SFTP status error — `status_code`, `headers`,
`cookies`, `text` and `json` are populated exactly as on the success path:

```python
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='https://api.example.com/v1/items/missing',
    protocol='HTTPS',
    protocol_info={'request_type': 'get'},
)
assert result['ok'] is False
assert result['status_code'] == 404
assert result['error']['code'] == 'HTTP_STATUS'
assert result['json'] == {'detail': 'no such item'}   # the body survives
```

### `status_code` per protocol

There is no protocol-neutral status concept, so the library states one
explicitly rather than inventing a sentinel.

| Situation | `status_code` | `error['code']` | `ok` |
|---|---|---|---|
| HTTP / HTTPS / SOAP success | the real HTTP status | — | `True` |
| HTTP / HTTPS 4xx or 5xx | the real HTTP status | `HTTP_STATUS` | `False` |
| SOAP Fault (any status, **including 200**) | the real HTTP status | `SOAP_FAULT` | `False` |
| FTP success | the server's reply code (2xx), else `200` | — | `True` |
| FTP failure | the server's reply code (4xx/5xx), else `500` | `FTP_STATUS` | `False` |
| SFTP success | `200` | — | `True` |
| SFTP `SSH_FX_NO_SUCH_FILE` | `404` | `SFTP_STATUS` | `False` |
| SFTP `SSH_FX_PERMISSION_DENIED` | `403` | `SFTP_STATUS` | `False` |
| SFTP other failure | `500` | `SFTP_STATUS` | `False` |
| SSH host-key verification failure | `495` *(library-assigned)* | `HOST_KEY` | `False` |
| Timeout (connect, read or total) | `504` | `TIMEOUT` | `False` |
| Circuit open | `503` | `CIRCUIT_OPEN` | `False` |
| DNS / connect / TLS failure | `502` | `DNS` / `CONNECT` / `TLS` | `False` |
| Response over the size cap | `502` | `RESPONSE_TOO_LARGE` | `False` |
| Multipart response nested past the depth cap | `502` | `RESPONSE_TOO_DEEP` | `False` |
| Dispatch exhausted the interpreter stack | `502` | `STACK_EXHAUSTED` | `False` |
| Body will not parse (response side) | `502` | `SERIALIZATION` | `False` |
| Body will not serialise (request side) | `400` | `SERIALIZATION` | `False` |
| XML refused before parse (DOCTYPE) | `502` | `XML_UNSAFE` | `False` |
| Path containment violation | `400` | `PATH` | `False` |
| Caller configuration error | `400` | `CONFIG` | `False` |
| Your own processor callback failed | `500` | `PROCESSOR` | `False` |

`495` is library-assigned and documented: no registered status describes an SSH
host-key mismatch, and 495 — a client-certificate rejection — is the nearest
honest neighbour.

---

## Error handling

Two things can happen to a failing call, and **placement decides which**.

**A configuration error raises**, synchronously, before anything is dispatched.
These are programming errors on your side and are never retryable, so they
escape rather than becoming an envelope a retry loop would re-attempt forever:

```python
from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.utils.exceptions import ConfigurationError

try:
    await request(
        url='http://api.example.com/v1/items',
        protocol='HTTPS',                      # HTTPS will not dispatch http://
        protocol_info={'request_type': 'get'},
    )
except ConfigurationError as exc:
    print('bad configuration:', exc)
```

The errors that escape this way are: a `protocol` that is not a registered name;
a `protocol_info` that is not a mapping or that omits a required key; an
unknown top-level keyword or security-sensitive protocol option; a URL whose
scheme the protocol will not dispatch on; an HTTP `request_type` outside the
allowlist; an `http_file_upload_config` combined with a GET; an `auth` carrying
no `login` and `password` on **FTP**; invalid SFTP auth with no username or no
explicit password/client key; and
every other malformed value the HTTP and SOAP constructors check.

**Everything else is an `ok=False` envelope** — every remote failure, every
transport failure, and the four configuration keys two protocols defer by
contract: FTP's `command` and `server_path`, and SFTP's `mode` and
`remote_path`. Those arrive as `error['code'] == 'CONFIG'` with status 400.

Why those four and nothing else: `protocol_info` is optional for FTP and SFTP,
so their request objects must stay constructible without one — which puts the
check after construction, and construction is what runs outside the `try`. All
four are still checked **before** their protocol opens a connection, so an
unreachable host never answers for your typo with a `CONNECT`/502 you might
then retry.

**The rule in one line:** whether a configuration error raises or envelopes is
decided by *where* it is detected — in a protocol's constructor (raises) or once
its `handle_request` is running (envelopes) — never by which kind of mistake it
was.

Code that wants to handle both alike catches `ConfigurationError` **and**
branches on `result['error']['code']`.

### The exception hierarchy

Every class below is importable from `asyncio_gateway.utils.exceptions`:

```python
from asyncio_gateway.utils.exceptions import (
    AsyncGatewayError,
    CircuitOpenError,
    ConfigurationError,
    ConnectError,
    DnsError,
    FtpStatusError,
    GatewayTimeoutError,
    HostKeyError,
    HttpStatusError,
    LocalWriteError,
    PathContainmentError,
    ProtocolError,
    ResponseTooLargeError,
    SerializationError,
    SftpStatusError,
    SoapFaultError,
    TlsError,
    TransportError,
    UnsafeXmlError,
    UnsupportedVerbError,
)
```

```text
AsyncGatewayError                 GATEWAY              502
├── ConfigurationError            CONFIG               400
│   └── UnsupportedVerbError      CONFIG               400
├── SerializationError            SERIALIZATION        502 (400 request side)
│   └── UnsafeXmlError            XML_UNSAFE           502
├── PathContainmentError          PATH                 400
├── LocalWriteError               PATH                 400
├── TransportError                TRANSPORT            502
│   ├── ConnectError              CONNECT              502
│   ├── DnsError                  DNS                  502
│   ├── TlsError                  TLS                  502
│   ├── HostKeyError              HOST_KEY             495
│   ├── GatewayTimeoutError       TIMEOUT              504
│   ├── ResponseTooLargeError     RESPONSE_TOO_LARGE   502
│   └── ResponseTooDeepError      RESPONSE_TOO_DEEP    502
├── ProcessorError                PROCESSOR            500
├── CircuitOpenError              CIRCUIT_OPEN         503
├── StackExhaustedError           STACK_EXHAUSTED      502
└── ProtocolError                 PROTOCOL             502
    ├── HttpStatusError           HTTP_STATUS          the real status
    ├── FtpStatusError            FTP_STATUS           the reply code
    ├── SftpStatusError           SFTP_STATUS          404 / 403 / 500
    └── SoapFaultError            SOAP_FAULT           the real status
```

`AsyncGatewayError` is the one base to catch. Instances pickle cleanly, status
included, so they survive a `ProcessPoolExecutor` or a Celery boundary.

### The error-code table

`error['code']` values are **wire-stable**: the human-readable `message` may
change freely between releases, the code may not. Branch on the code, never on
message text.

| `code` | Raised when | Retryable? |
|---|---|---|
| `CONFIG` | Your configuration cannot form a valid call | No |
| `PROCESSOR` | Your own pre/post-processor callback failed | No |
| `SERIALIZATION` | A body will not parse, or will not serialise | No |
| `XML_UNSAFE` | An XML prolog declares a DOCTYPE | No |
| `PATH` | A local path escapes its target directory or is a symlink (`PathContainmentError`), or the local filesystem refused the write — missing parent, unwritable directory, full disk (`LocalWriteError`) | No |
| `TRANSPORT` | Any other client-side transport failure | Sometimes |
| `CONNECT` | The connection was refused or reset | Yes |
| `DNS` | The host name does not resolve | Yes, transiently |
| `TLS` | A TLS handshake or certificate check failed | No |
| `HOST_KEY` | An SSH host key is unknown or does not match | No |
| `TIMEOUT` | A connect, read or total deadline expired | Yes |
| `RESPONSE_TOO_LARGE` | The body exceeded `max_response_bytes` | No |
| `RESPONSE_TOO_DEEP` | A multipart body nested past the 64-level depth cap | No |
| `STACK_EXHAUSTED` | Dispatching the call exhausted the interpreter stack | No |
| `CIRCUIT_OPEN` | The breaker for this destination is open | Later |
| `HTTP_STATUS` | An HTTP 4xx or 5xx | Depends |
| `FTP_STATUS` | An FTP 4xx or 5xx reply | Depends |
| `SFTP_STATUS` | An SFTP `SSH_FX_*` failure | Depends |
| `SOAP_FAULT` | A SOAP Fault, at any status | Depends |
| `GATEWAY` / `PROTOCOL` | A base class raised directly | — |

**A library bug is not a failed request.** A `KeyError`, a `TypeError`, an
`asyncio.CancelledError` — anything that is not an `AsyncGatewayError` —
propagates to you unchanged rather than being reported as a network failure.
That is deliberate: reporting library bugs as failed calls is what hid most of
this package's defects for its whole history.

---

## Retry, timeout and circuit-breaker behaviour

### Timeouts

`protocol_info['timeout']` defaults to **15 seconds** and must be a positive
number. What it bounds differs per protocol:

| Protocol | What `timeout` bounds |
|---|---|
| HTTP / HTTPS / SOAP | The **whole exchange**, redirect chain included — one budget spent across every hop, not handed afresh to each. |
| FTP | The connect, and **each** individual socket read and write — not the transfer as a whole. |
| SFTP | The connect and the login. asyncssh offers no transfer deadline. |

The HTTP wording is precise and load-bearing: a per-hop deadline would let a
`max_redirects=10` chain run for eleven times the deadline you set. It does not.

### Retries

Retries are **off by default**. Ask for them with a `retry_config` inside
`circuit_breaker_config`:

```python
from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.utils.exceptions import GatewayTimeoutError, TransportError

result = await request(
    url='https://api.example.com/v1/items',
    protocol='HTTPS',
    protocol_info={
        'request_type': 'get',
        'circuit_breaker_config': {
            'maximum_failures': 5,
            'timeout': 60,
            'retry_config': {
                'name': 'items-api',
                'allowed_retries': 2,
                'backoff': 'exponential',
                'delay': 0.1,
                'max_delay': 10,
                'jitter': True,
                'retriable_exceptions': [TransportError, GatewayTimeoutError],
            },
        },
    },
)
assert result['ok'] is True
```

`circuit_breaker_config` keys — an unknown key is **rejected by name**, not
silently defaulted:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `maximum_failures` | number ≥ 0 | `5` | Consecutive failures that open the circuit. An explicit `0` is honoured. |
| `timeout` | number ≥ 0 | `60.0` | Seconds an open circuit stays open before one half-open trial. |
| `retry_config` | `dict` | absent | Retry settings; absent means no retries. |
| `clock` | callable | `time.monotonic` | The monotonic clock. Part of the interface, not a test-only door. |
| `sleep` | async callable | `asyncio.sleep` | The backoff wait. |

`retry_config` keys — likewise closed:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str` | — | A label for this policy. |
| `allowed_retries` | `int` | — | Retries **past** the first attempt. `0` means one attempt. |
| `backoff` | `'exponential'` or `'constant'` | `'exponential'` | The wait shape. Any other value raises. |
| `delay` | number | `0.1` | Base seconds between attempts. |
| `max_delay` | number | `10.0` | Ceiling on one wait. |
| `jitter` | `bool` | `True` | Randomise each wait. |
| `retriable_exceptions` | list of exception **classes** | every failure | What a retry is attempted for. |
| `abortable_exceptions` | list of exception **classes** | see below | What stops the loop dead. |
| `on_retries_exhausted` | callable | `None` | Invoked when retries run out. |
| `on_failed_attempt` | callable | `None` | Invoked per failed attempt. |
| `on_abort` | callable | `None` | Invoked on an abort. |

`backoff` is selected by **this named key**. `delay` and `max_delay` are non-zero
by default and jitter is on, because a zero base with a zero ceiling produces
immediate, un-spaced, perfectly synchronised retries against a dependency that
is already failing — the thundering herd, and the one shape a retry must never
take.

`ConfigurationError` and `ResponseTooLargeError` abort by default and propagate
as themselves. Retrying them cannot help, and *counting* them would be worse:
one caller passing a bad verb would drive the destination's circuit open for
everyone else in the process.

### The circuit breaker

The breaker is keyed **per destination** — `(family, host, port)` — in a
process-wide registry, so failures accumulate across `request()` calls (which is
what makes a circuit able to open at all) without one flaky host opening the
circuit for every other. The registry holds 256 destinations and evicts the
least recently used.

Three states: **closed** (calls pass, failures counted), **open** (calls refused
with `CIRCUIT_OPEN`/503 until the timeout elapses), **half-open** (exactly one
trial call — its success closes the circuit, its failure re-opens it).

---

## Transport security

### TLS for HTTP, HTTPS and SOAP

`verify_ssl` defaults to `True`. A client certificate is a
`(certificate path, key path)` **pair**:

```python
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='https://api.example.com/v1/items',
    protocol='HTTPS',
    protocol_info={
        'request_type': 'get',
        'certificate': (CLIENT_CERT_PATH, CLIENT_KEY_PATH),
    },
)
```

The context is built for `ssl.Purpose.SERVER_AUTH`, so it verifies the server's
chain **and** its host name while presenting your certificate. Supplying a
certificate overrides `verify_ssl=False`: a caller who wants no verification
gets it by supplying no certificate. A passphrase-protected key is refused
rather than prompting. A `certificate` that is not a loadable pair raises
`ConfigurationError` — your typo, reported at 400 and never retried.

### FTPS

`verify_ssl` defaults to `True` for FTP too, matching HTTP: FTP sends its
credentials as literal `USER`/`PASS` lines, and a default that puts your
password on the wire is not a default anyone asked for.

**It fails closed rather than downgrading.** A server that offers no TLS fails
with `error['code'] == 'TLS'`; it never completes in plaintext. The value handed
to the transport is never `None`, which is how `aioftp` is told to speak
plaintext.

`verify_ssl=False` is honoured, **is unsafe**, and logs a warning naming the risk
on every use: the session is opened in plaintext, so the credentials and every
byte transferred cross the network in the clear.

**It speaks implicit FTPS, not explicit FTPS.** The TLS value is handed to
`aioftp.Client(ssl=...)`, which wraps the control connection from the first
byte — implicit FTPS, conventionally port 990. *Explicit* FTPS, the more common
deployment, connects in plaintext on port 21 and issues `AUTH TLS` to upgrade;
this library never sends that command, so pointing it at an explicit-FTPS
server fails the handshake with `error['code'] == 'TLS'` (it reads the server's
plaintext `220` greeting where a ServerHello belongs).

That failure is safe — it fails closed, and no credentials are sent — but it
does mean this client interoperates with implicit FTPS only. If you need
explicit FTPS, say so on the issue tracker; the change is confined to calling
`aioftp`'s `upgrade_to_tls()` after a plaintext connect.

### SFTP host keys

**Verification is on by default.** Omitting `known_hosts` is how asyncssh is
asked for its own documented `~/.ssh/known_hosts` resolution — this library
passes no value rather than restating a decision it does not own.

Pin explicitly with **exactly one** of these three keys. They are alternatives,
not layers: naming two is a caller asking for two policies at once and is
rejected.

| Key | Meaning |
|---|---|
| `known_hosts` | Anything asyncssh accepts — a path, key data, a callable. |
| `host_key` | One trusted server key, expressed for you in asyncssh's `(host keys, CA keys, revoked keys)` pinning form. |
| `insecure_skip_host_key_check` | The bypass. See below. |

A mismatched or untrusted key fails with `error['code'] == 'HOST_KEY'` and status
495, and the message names the option that fixes it — so a host simply absent
from `~/.ssh/known_hosts` (a container, every time) tells you what to do.

**The bypass must be asked for by its own name.** Only the literal boolean
`insecure_skip_host_key_check=True` disables verification, and it logs a warning
on every use. Nothing else reaches it: not any other truthy value, so a config
file's string `'false'` cannot turn verification off by being non-empty; not
`verify_ssl=False`, which is the HTTP family's TLS switch and says nothing about
SSH host keys; and not a `known_hosts` of `None`, which is asyncssh's spelling of
the same thing and is **rejected** rather than quietly honoured.

With verification off, any host that answers can impersonate the endpoint,
collect the credentials offered to it, and read or alter every byte transferred.

**No ambient identity is ever offered.** By default this library passes
`client_keys=None` and `agent_path=None`, so asyncssh reads neither the host's
default private keys nor `SSH_AUTH_SOCK`. Do key-based authentication with
`SFTPAuth`; agent use remains off unless `use_ssh_agent=True`:

```python
from asyncio_gateway import SFTPAuth
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='sftp.example.com',
    auth=SFTPAuth(
        username='deploy',
        client_keys=[CLIENT_SSH_KEY_PATH],
        key_passphrase='not-a-real-key-passphrase',
    ),
    protocol='SFTP',
    protocol_info={
        'mode': 'get',
        'remote_path': '/exports/report.csv',
        'local_path': LOCAL_DOWNLOAD_PATH,
        'known_hosts': KNOWN_HOSTS_PATH,
    },
)
assert result['ok'] is True
```

Set both `password` and `client_keys` when the server supports either method.
Legacy `.login`/`.password` objects and `protocol_info['client_keys']` remain
supported for 1.x callers, but two client-key sources in one call are rejected.

### Response size

`max_response_bytes` applies to **every** protocol, not only the HTTP family.
On HTTP and SOAP it caps the response body read; on FTP and SFTP it caps the
bytes a download writes to the local disk, counted across all files of a
recursive transfer rather than per file — the server chooses how many files it
sends, so a per-file allowance would bound nothing. A transfer that crosses the
ceiling is refused with `RESPONSE_TOO_LARGE` and leaves no file behind.

Every response read is capped at `max_response_bytes`, 64 MiB by default. A
declared `Content-Length` over the cap is refused before a byte is read; a body
that declares nothing is refused on the chunk that crosses it. There is
deliberately **no sentinel that disables the cap** — no 0, no -1, no None — so
"unbounded" is never one typo away. A caller who needs more names a bigger
number.

### Multipart nesting depth

A `multipart/*` response may nest — a part whose own media type is
`multipart/...` carries parts of its own — and this library descends into it
rather than refusing, because its leaves are ordinary parts carrying real
content. That descent is capped at **64 levels**, and the cap is separate from
`max_response_bytes` because the two bound different resources: 2000 levels of
empty nesting is 221 KB, nowhere near any realistic byte ceiling, and walking
it exhausted the interpreter's stack. A body past the cap answers
`RESPONSE_TOO_DEEP`/502; the level that would have overflowed is never entered.
Real MIME nests a handful of levels at most, so the cap is not reachable by
anything honest and there is no knob to raise it.

### Redirects

The redirect loop is this library's own, not aiohttp's, and that is what makes
`allowed_schemes` a guardrail rather than a decoration: **every hop** is checked
before it is issued, not only the initial URL. A hop to a scheme outside the
allowlist is refused with `CONFIG`/400 and no second request is made.

#### Headers on a cross-origin hop: an allowlist

**A hop that crosses an origin forwards only headers on a fixed allowlist.
Everything else — including your own custom headers — is dropped.** Your
`cookies` and the `auth` argument are dropped outright. A hop that stays on the
same origin forwards everything, unchanged.

These are the headers that survive:

| Group | Headers |
|---|---|
| Content negotiation | `Accept`, `Accept-Charset`, `Accept-Encoding`, `Accept-Language` |
| The body | `Content-Type`, `Content-Length`, `Content-Encoding`, `Content-Language`, `Content-Disposition` |
| Ranged reads | `Range` |
| Freshness | `Cache-Control`, `Pragma` |
| Client identity | `User-Agent` |

**Why an allowlist and not a list of secrets to strip.** A strip-list has to
name every credential header that exists, and the one that leaks is always the
one nobody thought of. This library shipped that list twice and leaked twice —
the second time it was the union of every credential set in the codebase and
still handed `X-Vault-Token`, `Private-Token`, `X-Goog-Api-Key` and seven more
to a hostile host, verbatim. Every vendor that invents a new auth header
silently re-opens the hole. Inverting the question makes a header this library
has never heard of **not cross**, which is the only version of the guard that
does not need to keep pace with the entire internet.

**If you need a custom header to survive**, name it — and only do this for a
header that is genuinely not a secret:

```python
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='https://api.example.com/v1/items',
    protocol='HTTPS',
    protocol_info={
        'request_type': 'get',
        'headers': {'X-Request-Id': 'correlation-id-1234'},
        'cross_origin_headers': ['X-Request-Id'],
    },
)
assert result['ok'] is True
```

`cross_origin_headers` widens the allowlist into the region this library has no
opinion about. It **cannot** re-admit a header known to be a credential —
`Authorization`, `Cookie`, `X-Api-Key`, `X-Vault-Token` and the rest are refused
with `ConfigurationError` rather than honoured, because that is not a trade-off
this library offers at any level of insistence.

This is also why a `session` you supply may not carry any header off the
allowlist: aiohttp merges session defaults into every request and no hop can
suppress them, so a hostile `Location` would receive them regardless of what the
loop decides. Pass headers per call instead — or, for a session default you have
declared in `cross_origin_headers`, the pair is accepted.

`max_redirects=0` and `allow_redirects=False` are **different asks**.
`max_redirects=0` is a bound the chain overran: a 302 answers `ok=False`,
`TRANSPORT`, status 302 and an empty `text`, because the body is never read.
`allow_redirects=False` hands you the redirect response itself: `ok=True`,
status 302 and the redirect's own body.

---

## You own URL validation

**`asyncio-gateway` will fetch whatever URL you give it. That is its purpose, not
a defect — and it means URL validation is yours.**

This library does not, and will not, decide whether a destination is one your
application ought to reach. It does not resolve host names to check them against
private ranges, it keeps no host allowlist, and it applies no SSRF heuristics.

**If any part of a URL you pass to `request()` comes from untrusted input — a
user-submitted webhook target, a field in an uploaded document, a redirect
target you read out of someone else's response — you must validate it before
calling.** Otherwise your service is an open proxy into whatever your network
can reach: cloud instance-metadata endpoints, internal admin interfaces,
databases bound to loopback.

What the library *does* give you is one **minimum, opt-in guardrail**:

```python
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='https://api.example.com/v1/items',
    protocol='HTTPS',
    protocol_info={
        'request_type': 'get',
        'allowed_schemes': {'https'},
    },
)
assert result['ok'] is True
```

`allowed_schemes` defaults to `{'http', 'https'}` and is enforced on the initial
URL **and on every redirect hop**. It bounds the *scheme*, and nothing else — it
says nothing about which hosts you reach. A richer guardrail (a host allowlist,
a caller-supplied validator hook) is deliberately out of scope for this release.

Note the shape: `allowed_schemes` takes a **collection**, and a bare string is
rejected — `frozenset('https')` is `{'h', 't', 'p', 's'}`, an allowlist that
admits no real scheme at all.

**A protocol-relative URL is refused.** `//host/p` names an authority but no
scheme, and this library will not guess one for you — the call raises
`ConfigurationError` (`code='CONFIG'`) before anything is dispatched, with a
message reading *"url is a protocol-relative reference, which names an authority
but no scheme to reach it over"*.

Prepending `https://` would be a guess about how to reach a host you named, and
a wrong guess is a plaintext request you did not ask for. Under `protocol='HTTPS'`
a URL with **no authority** is still upgraded — `host/p` becomes
`https://host/p`, because there is exactly one scheme that can satisfy `HTTPS`
and nothing about the destination is being guessed.

---

## Local files: downloads, uploads and overwrite

Every local file this library writes goes through one guarded path.

**Downloads refuse to overwrite by default.** An existing destination raises
`ConfigurationError`. Opt in by name with `overwrite=True` — available on
`download_file_from_url(...)`, in `http_file_download_config`, and in the FTP
and SFTP `protocol_info`:

```python
from asyncio_gateway.utils.http_file_config import download_file_from_url

await download_file_from_url(
    file_download_path=FILE_SOURCE_URL,
    local_filepath=LOCAL_DOWNLOAD_PATH,
    overwrite=True,
)
```

This is a **breaking change**, and it is why the examples here take their paths
from a variable rather than a fixed `/tmp/test.pdf`: a second run of an example
that hardcodes a path now fails by design.

**Files are created mode `0600`** — readable and writable by their owner and
nobody else, rather than at whatever `umask` allows.

**`preserve=True` preserves timestamps, never permissions or ownership.** SFTP
is the only protocol with the option (pass it in `additional_arguments`), and
asyncssh implements it by applying the *remote* file's attributes to the local
copy — including a `chmod` to the server's mode, which silently undid the `0600`
above: measured against a real server, a remote file at `0777` left the local
one at `0777`. The permission and ownership fields are therefore dropped and the
access/modification times are applied. A timestamp grants nobody anything; a
mode bit lets a remote endpoint decide the permissions of a file on your disk.
The other three protocols apply no server-supplied attribute to a local file, so
`0600` already held there unconditionally — all four now agree.

**A symbolic link at the destination is refused, never followed** — including
with `overwrite=True`, which opts into replacing a *file* and not into following
a link. The refusal is in the `os.open` flags (`O_EXCL`, `O_NOFOLLOW`), not in a
check before them: a pre-write `lstat` that likes what it sees and then opens
has a window between the two syscalls, and that window is the attack.

**Platform note.** `O_NOFOLLOW` is POSIX and absent on some platforms. Where it
is unavailable the write degrades to a pre-write `lstat` symlink check, which
**carries the time-of-check/time-of-use window the flag does not** — an
explicit, documented weakening rather than a silent one.

**Paths are canonicalised before they are opened**, so `..` and a symlinked
intermediate component resolve to where they actually point. On a recursive FTP
or SFTP directory download the **remote server** supplies the entry names, and
each composed path is checked against the directory your `client_path` /
`local_path` names — a hostile listing cannot write outside it.

Two more: a **relative** `local_filepath` resolves against the **process working
directory**, and a destination that is itself a **directory** is reported as a
directory rather than advising `overwrite=True`, which cannot help there.

**Relative local paths are accepted on every protocol, and all four resolve them
the same way** — against the process working directory. That applies to HTTP and
SOAP's `download_filepath` / `local_filepath`, FTP's `client_path`, and SFTP's
`local_path`. Containment is unaffected: the directory your relative path names
is still the boundary a hostile server's entry names are checked against, so a
relative destination is confined exactly as an absolute one is.

**Scope boundary on upload: the source side is not containment-checked.** The
guarantees above are about where a download is allowed to *write*. On an
**upload**, a `client_path` / `local_path` / `local_filepath` that is a symbolic
link pointing outside itself is followed, read, and sent — the caller named the
file to upload, and this library treats that as the caller's own decision rather
than a remote party's. If your process uploads paths that a *less-trusted* party
can influence, resolve and check them yourself before the call.

**A failure part-way through a download removes what it wrote** rather than
orphaning a partial file — and `delete_local_file_path` is idempotent so your
own cleanup can still run after it.

`http_file_download_config` keys: `download_filepath` (default
`'response.txt'`), `file_download_chunk_size` (default 65536), `overwrite`
(default False). `http_file_upload_config` keys: `local_filepath` and `file_key`
(both required), and `file_upload_chunk_size`, whose presence selects the
streaming upload path.

Uploads stream from disk and the body is rebuilt **per attempt and per redirect
hop**, so a retry sends the file rather than zero bytes.

---

## Redaction and the payload echo

The envelope is designed to be safe to log. Credential values are masked in the
returned `url`, in `headers`, in `cookies` (every value, since a cookie name
carries no reliable signal), in `error['message']` and `error['cause']`, and in
the failure log — by one implementation, so the log and the envelope cannot
disagree about what a secret is.

Add your own sensitive query-parameter names with
`protocol_info['redact_query_params']`. They are **added** to the built-in set,
never replacing it, and matched case-insensitively.

**The payload echo is bounded, and here is exactly how.** `payload` echoes back
what you sent, with mapping values masked **by key name** — `password`, `token`,
`api_key`, `secret` and their siblings — to a **depth of 4**. **Below depth 4,
and for a payload that is not a mapping, your own data is echoed back
verbatim.**

That bound is a deliberate contract, not an oversight: a key name proves nothing
about the value beneath it once you are deep inside an arbitrary structure. What
it means for you is concrete — **if you post a secret at nesting depth 5, or as
a bare string, or under a key name this library does not recognise as sensitive,
it will appear in `result['payload']`, and anything you log that envelope to
will receive it.**

```python
from asyncio_gateway.asyncio_gateway import request

result = await request(
    url='https://api.example.com/v1/items',
    data={
        'password': 'masked-at-the-top-level',
        'a': {'b': {'c': {'token': 'still-masked-at-depth-4'}}},
        'w': {'x': {'y': {'z': {'token': 'ECHOED-VERBATIM-BELOW-DEPTH-4'}}}},
    },
    protocol='HTTPS',
    protocol_info={'request_type': 'post'},
)
echo = result['payload']
assert echo['password'] == '***redacted***'
assert echo['a']['b']['c']['token'] == '***redacted***'

# One level deeper, the bound has been spent — and this is your data,
# in the envelope, exactly as you sent it.
assert echo['w']['x']['y']['z']['token'] == 'ECHOED-VERBATIM-BELOW-DEPTH-4'
```

---

## Logging

Every module logs through `logging.getLogger(__name__)`, so all records land
under the `asyncio_gateway` tree and your application configures or silences the
library with one call. A `NullHandler` is attached and nothing else: until you
add a handler, the library's records go nowhere, which is the correct default
for a library.

Each failure is logged exactly **once**, at the one conversion point.
Remote-side failures a caller may legitimately expect — a 4xx, a Fault, an open
circuit — log at `warning`; transport and configuration failures log at `error`.
A configuration error that *escapes* to you is never logged, because you are
already being told about it.

**The traceback is carried as a redacted string in `extra['traceback']`, not via
`exc_info=True`** — a deliberate trade, and yours to weigh. A live `exc_info` is
formatted by whichever handler *your* application installed, from the exception
objects themselves, and the chained `aiohttp` exception at the bottom of a
transport failure stringifies to the **unredacted URL**, query string and all.
That cannot be masked after the fact, so the traceback is rendered and redacted
before the record leaves the library.

The cost: a handler reading `record.exc_info` finds nothing, so **APM tools that
group exceptions natively — Sentry, Datadog and the like — will not group these
failures**. If your deployment sends no credentials in URLs and you would rather
have native grouping, the revert is one line in
`asyncio_gateway/asyncio_gateway.py`'s `log_failure`: replace the `'traceback'`
entry in `extra` with `exc_info=exc` on the `logger.log` call. Choose knowingly
— it re-opens the leak the current form closes.

---

## Supported Python versions

**Python 3.10 and newer.** Tested on 3.10, 3.11, 3.12, 3.13 and 3.14; every one
of them is a required check in CI.

asyncio throughout — there is no synchronous entry point and none is planned.

---

## Versioning policy

[Semantic versioning](https://semver.org/). `1.0.0` is the first release under
the name `asyncio-gateway`; it follows `asyncio-requests 2.7.3` under the old
name, and the reset is explained in
[Migrating from `asyncio-requests`](#migrating-from-asyncio-requests). Given
`MAJOR.MINOR.PATCH`:

- **MAJOR** — a breaking change to the public surface: `request()`'s signature,
  the envelope's key set, an `error['code']` value, or a documented default that
  changes behaviour.
- **MINOR** — new protocols, new `protocol_info` keys, new exported helpers.
  Backwards compatible.
- **PATCH** — bug fixes and documentation.

**`error['code']` values are wire-stable within a major version.** Messages are
not: branch on the code, never on the message text.

The public surface is `asyncio_gateway.asyncio_gateway.request()`, the envelope in
`asyncio_gateway.utils.envelope`, the exception hierarchy in
`asyncio_gateway.utils.exceptions`, the helpers in
`asyncio_gateway.utils.http_file_config`, and
`asyncio_gateway.utils.request_tracer`. Anything under
`asyncio_gateway.helpers.internal` is internal and may change in a patch release.

### Cutting a release

Releases are automated by
[`.github/workflows/publish.yml`](.github/workflows/publish.yml). A maintainer
edits two files and merges; the workflow builds, publishes to PyPI, tags the
commit, and writes the GitHub Release. There is no manual `python -m build`,
no `twine upload`, and no hand-written `git tag`.

The version lives in **exactly one file**: `pyproject.toml`. Bump `version`
there and nowhere else.

```text
# 1. Bump `version` in pyproject.toml. That is the only file to edit for it.
# 2. In CHANGELOG.md, replace `unreleased` in that version's heading with the
#    release date:  ## [1.1.0] — unreleased   ->   ## [1.1.0] — 2026-08-18
# 3. Merge to the default branch. That is the whole release.
```

What happens then, in order:

| Job | Does | Gates the next on |
|-----|------|-------------------|
| `release-gate` | Reads the name and version from `pyproject.toml`, asks PyPI whether that exact version exists, and refuses to continue if the CHANGELOG heading still says `unreleased`. | The version being new **and** the changelog being dated. |
| `ci-gate` | Asks the Actions API whether every `ci.yml` run for this exact commit finished green. | CI having actually passed. |
| `build` | `python -m build` plus `twine check`, uploading the artifacts. | A valid wheel and sdist. |
| `publish` | Uploads to PyPI over Trusted Publishing (OIDC). No API token is stored anywhere. | A successful upload. |
| `github-release` | Creates the annotated tag `vX.Y.Z` and a GitHub Release whose body is that version's CHANGELOG section. | — |

Two consequences worth knowing:

- **A merge that does not bump the version is a clean no-op**, not a failure.
  PyPI versions are immutable, so `release-gate` skips when the declared
  version is already published. This is why every merge can safely trigger the
  workflow.
- **Forgetting the CHANGELOG date blocks the release rather than mis-shipping
  it.** Publishing a version whose own changelog says it is unreleased would be
  incoherent and unfixable — a PyPI version cannot be re-uploaded once spent —
  so the workflow stops and tells you which heading to edit.

**One-time setup before the first release.** `asyncio-gateway` has never been
uploaded, so Trusted Publishing needs a
[pending publisher](https://pypi.org/manage/account/publishing/) registered by
hand first: project `asyncio-gateway`, owner `ajyadav013`, repository
`asyncio-gateway` (the distribution was renamed first, because PyPI rejected
`async-gateway` under PEP 503 normalisation against the existing
`asyncgateway`; the **repository** was renamed afterwards to match, so the two
names agree today), workflow `publish.yml`, environment `pypi` — and a
GitHub
environment named `pypi` on the repository. Without it the first upload fails
with `invalid-publisher`. The header comment in the workflow says the same
thing, at the place where someone debugging that failure will look.

To build the artifacts locally — for inspection, not for publishing — note that
`build` is not part of the `dev` extra and must be installed separately:

```text
pip install build twine
python -m build
twine check dist/*
```

---

## Contributing

```text
git clone https://github.com/ajyadav013/asyncio-gateway
cd asyncio-gateway
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

The three commands you will reach for while iterating:

```text
pytest
flake8 asyncio_gateway tests
mypy asyncio_gateway
```

**Those three are not the full gate.** CI enforces about a dozen required
checks, and several exist only as inline steps in
`.github/workflows/ci.yml` — the unjustified-suppression check, the two
coverage-pragma checks, the assertion that `ignore_errors` has not come
back, `bandit`, and the clean-venv install of the built wheel and sdist.
None of them is part of `pytest`, `flake8` or `mypy`, so a green run of
the three above can sit on a branch that CI fails. That is not
hypothetical: it is what happened, and it is why the script below exists.

Before pushing, run every gate CI runs, in one command:

```text
scripts/ci-local.sh              # every locally-runnable gate
scripts/ci-local.sh --fast       # skip the slow ones (build, 5 seeds)
PYTHON=.venv/bin/python scripts/ci-local.sh
```

It runs each gate, keeps going after a failure, and prints a
pass/skip/fail summary, so one local pass tells you everything CI would.
Exit status is 0 only when every gate it ran passed.

Two things it deliberately does **not** do. It never installs into your
venv — the artifact gates need `build` and `twine`, which cannot coexist
with the dev extra's `setuptools<76` pin, so it uses them when present
and says so plainly when they are absent. And it runs on one interpreter:
the **3.10–3.14 matrix, the pull-request changelog check, and the monthly
newer-CPython check remain CI-only**, each annotated in the script with
the reason. `tests/test_ci_local.py` parses the script and the workflow
together and fails if a CI step is in neither the covered nor the
CI-only list, so the two cannot drift apart silently.

There is **no autoformatter** configured. Match the surrounding file by hand;
`flake8` judges the result.

### Docker: the wheel, and the four protocols against real servers

Two assets, answering two different questions. Neither is a way to *deploy*
this library — it is a library, there is nothing to serve — and both exist
because a development checkout structurally cannot answer them.

**Does the built artifact work?** `Dockerfile` builds the wheel, installs it
into a clean image, and runs the whole suite against the *installed
distribution* rather than the source tree:

```text
docker build -t asyncio-gateway:test .
docker run --rm asyncio-gateway:test
```

This is the check that catches a packaging defect an editable install hides —
an undeclared dependency imports fine in a tree that already has it, and fails
on the first `import` for everyone else. `docker/run-tests.sh` refuses to start
unless `asyncio_gateway` resolves out of `site-packages`, so a green run cannot
have quietly tested the checkout.

**Do the protocols actually work?** `docker-compose.yml` stands up an HTTP/HTTPS
server, a second HTTP origin, an implicit-FTPS server, an OpenSSH SFTP server
and a SOAP endpoint, mints a throwaway CA for the TLS ones, and runs
`docker/integration/test_integration.py` against all of them:

```text
docker compose up --build --abort-on-container-exit --exit-code-from integration integration
docker compose down -v
```

The unit suite covers the same code far more exhaustively, but against doubles.
These are the claims only a live peer can settle: that FTPS is really FTPS and
is refused when downgraded, that an unknown SSH host key is really refused and
the same connect succeeds once pinned, that an upload puts the **local** file's
bytes at the **remote** path, and that a cross-origin redirect arrives at the
second origin carrying no credentials — observed at that server, not asserted
in-process.

House rules worth knowing before you open a pull request:

- **Every public function carries a docstring** documenting its arguments,
  return value and the errors it raises, and full type annotations.
- **Coverage is ratcheted.** `fail_under` in `pyproject.toml` may be raised,
  never lowered.
- **No error suppression.** No bare `except`, no `# type: ignore` to silence a
  real mismatch, no lint disable without a stated reason.
- **README examples are executed by the test suite.** If you change a signature,
  a default or an envelope key, `tests/test_docs.py` will tell you which part of
  this document went stale — that is what it is for.

Issues and pull requests: <https://github.com/ajyadav013/asyncio-gateway>.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## Licence and attribution

MIT — see [LICENSE](LICENSE).

Copyright (c) 2026 Arjunsingh Yadav, and Copyright (c) 2022 Fynd and
contributors to this fork. Both notices are reproduced here because `LICENSE`
carries both: this project began as a fork of Fynd's `aio-requests`, and MIT
requires the original notice to travel with the code.

That fork was published as
[`asyncio-requests`](https://pypi.org/project/asyncio-requests/) (12 releases,
2022-02-24 to 2023-01-02, last at `2.7.3`), authored by Arjunsingh Yadav,
Manish Magnani and Devesh Ratthour at Fynd. `asyncio-gateway` is its
continuation under a new name — see
[Migrating from `asyncio-requests`](#migrating-from-asyncio-requests).
