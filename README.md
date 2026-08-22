# asyncio-gateway

One `await` for `HTTP`, `HTTPS`, `FTP`, `SFTP`, `SOAP`, `JSONRPC`, `GRAPHQL`, `S3`, and `GRPC`.

`asyncio-gateway` is a typed Python client facade for outbound network and
file-transfer work. Every selector returns the same response envelope, uses the
same retry and circuit-breaker vocabulary, preserves cancellation, and applies
bounded reads plus redaction at the edges.

Use it when an application talks to several kinds of remote systems and you
want one predictable success check—`result['ok']`—without hiding the protocols'
real wire semantics.

- [Choose a protocol](#quickstart-per-protocol)
- [Use the public API](#public-api)
- [Read success and failure](#the-response-envelope)
- [Configure resilience](#reliability)
- [Apply the security boundaries](#security-boundaries)

## Install

```text
python -m pip install asyncio-gateway
```

The gRPC adapter is included in the normal installation through the bounded
runtime dependency `grpcio>=1.83.0,<2`. JSON serialization uses `orjson`.

## Supported Python versions

Python 3.10 and newer are supported.

## Start with one request

Import `request()` from `asyncio_gateway.asyncio_gateway`; the package root is
metadata-only.

```python
import asyncio

from asyncio_gateway.asyncio_gateway import request

async def main() -> None:
    result = await request(
        url='https://api.example.com/v1/items',
        protocol='HTTPS',
        protocol_info={'request_type': 'GET'},
    )

    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['json'] == {'items': []}


if __name__ == '__main__':
    asyncio.run(main())
```

That block is a complete script. Shorter snippets below assume they already run
inside an async function, test, notebook, or framework lifecycle.

Protocol selectors ignore surrounding whitespace and case. Prefer the
uppercase spellings in configuration because they match the public registry.

| Need | Selector | Target shape | Body meaning |
|---|---|---|---|
| Web API | `HTTP` / `HTTPS` | `https://host/path` | query, JSON, form, XML, bytes, or streamed file |
| File transfer | `FTP` | bare host | unused; operation lives in `protocol_info` |
| SSH file transfer | `SFTP` | bare host | unused; operation lives in `protocol_info` |
| SOAP service | `SOAP` | HTTP(S) URL | XML text or an XML `Element` |
| JSON-RPC 2.0 | `JSONRPC` | explicit HTTP(S) URL | params mapping, list, or `None` |
| GraphQL | `GRAPHQL` | explicit HTTP(S) URL | variables mapping or `None` |
| AWS object storage | `S3` | `s3://bucket/key` | unused; fixed command in `protocol_info` |
| Raw unary gRPC | `GRPC` | `grpc://host:port` or `grpcs://host:port` | bytes, or serializer input |

## Quickstart per protocol

### HTTP and HTTPS

`HTTP` accepts both HTTP and HTTPS URLs. `HTTPS` requires TLS and upgrades a
schemeless host to `https://`. An explicit plaintext URL under `HTTPS` is a
configuration error. Authentication is either `None` or `aiohttp.BasicAuth`;
supplying both explicit auth and URL userinfo is rejected.

```python
from asyncio_gateway.asyncio_gateway import request

created = await request(
    url='https://api.example.com/v1/items',
    data={'name': 'widget'},
    protocol='HTTPS',
    protocol_info={'request_type': 'POST'},
)

assert created['ok'] is True
assert created['status_code'] == 201
assert created['json']['id'] == 'w-1'
```

Remote HTTP failures remain useful responses:

```python
from asyncio_gateway.asyncio_gateway import request

missing = await request(
    url='https://api.example.com/v1/items/missing',
    protocol='HTTPS',
    protocol_info={'request_type': 'GET'},
)

assert missing['ok'] is False
assert missing['status_code'] == 404
assert missing['error']['code'] == 'HTTP_STATUS'
assert missing['json'] == {'detail': 'no such item'}
```

TLS mismatches fail before a socket is opened:

```python
from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.utils.exceptions import ConfigurationError

try:
    await request(
        url='http://api.example.com/v1/items',
        protocol='HTTPS',
        protocol_info={'request_type': 'GET'},
    )
except ConfigurationError:
    pass
else:
    raise AssertionError('HTTPS accepted a plaintext target')
```

**HTTP verbs.**

| Verb | Typical use |
|---|---|
| `delete` | Delete a resource |
| `get` | Read a resource |
| `head` | Read metadata only |
| `options` | Discover server capabilities |
| `patch` | Partially update a resource |
| `post` | Create or invoke |
| `put` | Replace a resource |

### REST

REST is ordinary `HTTP`/`HTTPS` usage with application-specific resource
conventions; it is not a selector of its own.

```python
from asyncio_gateway.asyncio_gateway import request

items = await request(
    url='https://api.example.com/v1/items',
    protocol='HTTPS',
    protocol_info={'request_type': 'GET'},
)
assert items['ok'] and items['json']['items'] == []
```

### JSON-RPC 2.0

JSON-RPC is POST-only, redirect-free, single-call JSON-RPC 2.0. A method and a
string or integer request ID are required; batches and notifications are not
supported. `data` becomes `params`, and `None` omits `params` entirely.

```python
from asyncio_gateway.asyncio_gateway import request

rpc = await request(
    url='https://api.example.com/rpc',
    data=[2, 2],
    protocol='JSONRPC',
    protocol_info={'method': 'math.add', 'request_id': 1},
)

assert rpc['ok'] is True
assert rpc['protocol_details'] == {'id': 1, 'result': 4}
```

Run the repository's loopback client example with:

```text
python examples/jsonrpc_example.py http://127.0.0.1:8080/rpc
```

### GraphQL

GraphQL is POST-only and redirect-free. Put the query document in
`protocol_info['query']` and variables in `data`. A response carrying GraphQL
`errors` is a semantic failure even when the HTTP status is successful; any
partial `data` remains in `protocol_details`.

```python
from asyncio_gateway.asyncio_gateway import request

graphql = await request(
    url='https://api.example.com/graphql',
    data={'id': 'w-1'},
    protocol='GRAPHQL',
    protocol_info={
        'query': 'query Widget($id: ID!) { widget(id: $id) { id } }',
        'operation_name': 'Widget',
    },
)

assert graphql['ok'] is True
assert graphql['protocol_details']['data']['widget']['id'] == 'w-1'
```

The repository script is another loopback client:

```text
python examples/graphql_example.py http://127.0.0.1:8080/graphql
```

Subscriptions, GET queries, multipart upload, and incremental/deferred
streaming are intentionally outside this adapter.

### FTP and FTPS

FTP targets are bare hosts. Authentication must expose string `login` and
`password` attributes. TLS verification is on by default. A named
`tls_mode='implicit'` starts under TLS; `tls_mode='explicit'` connects, performs
the verified AUTH TLS upgrade, and only then logs in.

```python
import tempfile
from pathlib import Path
from types import SimpleNamespace

from asyncio_gateway.asyncio_gateway import request

with tempfile.TemporaryDirectory() as temp_dir:
    local_download = Path(temp_dir) / 'report.csv'
    download = await request(
        url='api.example.com',
        protocol='FTP',
        auth=SimpleNamespace(login='demo', password='secret'),
        protocol_info={
            'command': 'download',
            'server_path': '/reports/q1.csv',
            'client_path': str(local_download),
            'tls_mode': 'explicit',
            'verify_ssl': True,
            'overwrite': True,
        },
    )

    assert download['ok'] is True
    assert download['protocol_details']['tls_mode'] == 'explicit'
```

When `tls_mode` is omitted, legacy behavior is preserved:
`verify_ssl=True` means implicit FTPS and `verify_ssl=False` means plaintext
FTP with a warning. Named modes refuse unverified TLS, and explicit mode does
not accept a client certificate.

**FTP commands**

| Command | Direction or effect |
|---|---|
| `download` | remote `server_path` to local `client_path` |
| `upload` | local `client_path` to remote `server_path` |
| `remove` | remove the remote path |
| `remove_directory` | remove a remote directory |
| `remove_file` | remove a remote file |

### SFTP

SFTP takes a bare host and an `SFTPAuth` value. It supports password, explicit
client key, or both; legacy objects with `.login` and `.password` remain
compatible. SSH-agent use is disabled unless `use_ssh_agent=True` is chosen
explicitly. Host-key verification is enabled by default through asyncssh's
normal known-hosts resolution. Choose exactly one explicit policy:
`known_hosts`, a pinned `host_key`, or the loudly named
`insecure_skip_host_key_check=True`. A host-key mismatch reports `HOST_KEY`.

```python
import tempfile
from pathlib import Path

from asyncio_gateway import SFTPAuth
from asyncio_gateway.asyncio_gateway import request

with tempfile.TemporaryDirectory() as temp_dir:
    local_download = Path(temp_dir) / 'report.csv'
    known_hosts = globals().get(
        'KNOWN_HOSTS_PATH', str(Path.home() / '.ssh' / 'known_hosts'))
    sftp = await request(
        url='api.example.com',
        protocol='SFTP',
        auth=SFTPAuth(username='demo', password='secret'),
        protocol_info={
            'mode': 'get',
            'remote_path': '/reports/q1.csv',
            'local_path': str(local_download),
            'known_hosts': known_hosts,
            'overwrite': True,
        },
    )

    assert sftp['ok'] is True
    assert sftp['protocol_details']['mode'] == 'get'
```

**SFTP modes**

| Mode | Direction or effect |
|---|---|
| `get` | remote to local |
| `put` | local to remote |
| `remove` | remove the remote path |

### S3

S3 accepts only `s3://bucket[/key-or-prefix]` and four fixed commands:
download, upload, head, and one-page list. It has no arbitrary SDK dispatch.
The standalone demonstration uses a deterministic SDK double and therefore
needs neither AWS credentials nor network access:

```text
python examples/s3_example.py
```

Uploads are bounded and read into memory. Downloads require a destination,
enforce a positive cap, and never overwrite. Listing returns one page with at
most 1,000 items.

```python
from asyncio_gateway.asyncio_gateway import request

async def read_object_metadata():
    return await request(
        url='s3://example-bucket/reports/q1.csv',
        protocol='S3',
        protocol_info={'command': 'head', 'region': 'eu-west-1'},
    )
```

The script command above is run from a repository checkout; installed wheels
do not include `examples/`.

### gRPC

The gRPC adapter is raw unary-unary transport. It takes bytes unless a
synchronous `request_serializer` is supplied, always retains the raw response
as base64 text, and optionally puts JSON-safe deserialized output in `json`.

The runnable example starts a local generic server and needs no generated
protobuf modules:

```text
python examples/grpc_example.py
```

```python
from asyncio_gateway.asyncio_gateway import request

async def ping_service():
    return await request(
        url='grpcs://api.example.com:443',
        data=b'ping',
        protocol='GRPC',
        protocol_info={'method': '/demo.Echo/Ping'},
    )
```

Run the script command from a repository checkout. It starts its own local
generic server; installed wheels do not include `examples/`.

There are no generated stubs, reflection, streaming, custom channel options,
custom trust roots, or channel pooling.

### SOAP

SOAP always uses POST over the shared HTTP transport. Supply XML text or an XML
`Element`; there is no dict-to-XML mapping. The client wraps the body, applies
version-correct headers, parses a Fault, and returns the first response Body
child as `protocol_details['soap_body']`.

SOAP 1.1:

```python
from asyncio_gateway.asyncio_gateway import request

soap11 = await request(
    url='https://api.example.com/soap',
    data='<GetRate xmlns="urn:rates"><Pair>EURUSD</Pair></GetRate>',
    protocol='SOAP',
    protocol_info={
        'soap_version': '1.1',
        'soap_action': 'urn:rates#GetRate',
    },
)

assert soap11['ok'] is True
body = soap11['protocol_details']['soap_body']
assert body.findtext('{urn:rates}Value') == '1.09'
```

SOAP 1.2:

```python
from asyncio_gateway.asyncio_gateway import request

soap12 = await request(
    url='https://api.example.com/soap',
    data='<GetRate xmlns="urn:rates"><Pair>EURUSD</Pair></GetRate>',
    protocol='SOAP',
    protocol_info={
        'soap_version': '1.2',
        'soap_action': 'urn:rates#GetRate',
    },
)

assert soap12['ok'] is True
assert soap12['protocol_details']['soap_version'] == '1.2'
```

Faults preserve both the remote status and normalized SOAP details:

```python
from asyncio_gateway.asyncio_gateway import request

fault = await request(
    url='https://api.example.com/soap',
    data='<GetRate xmlns="urn:rates"><Pair>UNKNOWN</Pair></GetRate>',
    protocol='SOAP',
    protocol_info={'soap_version': '1.1'},
)

assert fault['ok'] is False
assert fault['status_code'] == 500
assert fault['error']['code'] == 'SOAP_FAULT'
```

`soap_body` is a raw `Element`, so normal ElementTree operations such as
`body.findtext('{urn:rates}Value')` apply. WSDL generation is not supported.
MTOM and `multipart/related` attachments are rejected with
`ConfigurationError` rather than partially interpreted.

## Public API

### `request()`

```python
from typing import Any, Dict

from asyncio_gateway.utils.envelope import GatewayResponse

async def request(
    url: str,
    data: object = None,
    auth: object = None,
    protocol: str = '',
    protocol_info: Dict[str, Any] | None = None,
    pre_processor_config: Dict[str, Any] | None = None,
    post_processor_config: Dict[str, Any] | None = None,
    **kwargs: Any,
) -> GatewayResponse: ...
```

| Argument | Meaning |
|---|---|
| `url` | HTTP(S), SOAP, JSON-RPC, and GraphQL URL; bare FTP/SFTP host; `s3://bucket/key`; or `grpc://host:port` / `grpcs://host:port` |
| `data` | HTTP/SOAP body, JSON-RPC/GraphQL params or variables, gRPC bytes/serializer input; `None` is meaningful for JSON-RPC/GraphQL |
| `auth` | HTTP accepts only `aiohttp.BasicAuth` or `None` and rejects URL-userinfo conflicts; FTP uses legacy `.login`/`.password`; SFTP accepts `SFTPAuth` password, key, or both plus legacy credentials; S3 accepts an access/secret object and `None` selects the AWS credential chain; gRPC auth must be None |
| `protocol` | One of `HTTP`, `HTTPS`, `FTP`, `SFTP`, `SOAP`, `JSONRPC`, `GRAPHQL`, `S3`, `GRPC` |
| `protocol_info` | Protocol-specific configuration mapping |
| `pre_processor_config` | Optional async processor run before dispatch |
| `post_processor_config` | Optional async processor run after dispatch |
| `kwargs` | Residual top-level keywords are rejected before processors or I/O; protocol options belong in `protocol_info` |

Processor configuration has the shape
`{'function': async_callable, 'params': {'name': value}}`. The callable receives
the live envelope as the keyword `response`; its return value lands in
`pre_processor_response` or `post_processor_response`. Processor failures raise
`ProcessorError`. A pre-processor may reshape payload and caller metadata, but
cannot remove required envelope keys or rewrite the dispatch `url` or
`protocol`. A post-processor runs after dispatch and may relabel report fields.

### File helpers

The helpers use the same guarded local-file primitives as protocol clients.

```python
from collections.abc import Mapping
from typing import Any

async def download_file_from_s3(
    *,
    bucket_name: str,
    s3_filepath: str,
    local_filepath: str,
    access_key: str | None = None,
    secret_key: str | None = None,
    region: str | None = None,
    overwrite: bool = True,
    max_response_bytes: int | None = None,
    **kwargs: Any,
) -> None: ...

async def download_file_from_url(
    file_download_path: str,
    local_filepath: str,
    request_type: str = 'get',
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    max_response_bytes: int = 67108864,
    chunk_size: int = 65536,
    overwrite: bool = False,
    **kwargs: Any,
) -> None: ...

async def delete_local_file_path(
    local_filepath: str,
    **kwargs: Any,
) -> None: ...
```

```python
import tempfile
from pathlib import Path

from asyncio_gateway.utils.http_file_config import download_file_from_url

with tempfile.TemporaryDirectory() as temp_dir:
    local_download = Path(temp_dir) / 'report.csv'
    await download_file_from_url(
        'https://api.example.com/report.csv',
        str(local_download),
        overwrite=True,
    )
    assert local_download.read_text() == 'quarter,total\nQ1,17\n'
```

```python
import tempfile
from pathlib import Path

from asyncio_gateway.utils.http_file_config import delete_local_file_path

with tempfile.TemporaryDirectory() as temp_dir:
    temporary_file = Path(temp_dir) / 'delete-me.txt'
    temporary_file.write_text('temporary', encoding='utf-8')
    await delete_local_file_path(str(temporary_file))
    assert not temporary_file.exists()
```

### S3 helper migration

The keyword-only `download_file_from_s3()` remains compatibility-oriented:
`overwrite=True` means it overwrites by default and
`max_response_bytes=None` means it is uncapped by default. Internally it uses
the shared hardened stream primitive. The `S3` selector deliberately chooses
the stricter policy: `overwrite=False` and a mandatory positive cap.

## Configuration reference

For legacy selectors, an ordinary unknown `protocol_info` key emits a
`DeprecationWarning` during the 1.x compatibility window. Non-string keys and
unknown names resembling authentication, TLS, redirect, header, cookie, or
host-key controls fail closed. The four additive selectors use closed
allowlists and reject every unknown key before processors or I/O.

### `protocol_info` — HTTP and HTTPS

| Key | Required | Default | Purpose |
|---|---:|---|---|
| `request_type` | yes | — | HTTP verb |
| `headers` | no | `{}` | Request headers |
| `cookies` | no | None | Request cookies; omission preserves a session cookie jar |
| `certificate` | no | `None` | Client certificate/key pair |
| `verify_ssl` | no | `True` | Verify server TLS |
| `trace_config` | no | a built-in tracer | aiohttp tracing; `[]` disables it |
| `timeout` | no | `15` | Whole-exchange seconds |
| `http_file_download_config` | no | `None` | Stream response to a local file |
| `http_file_upload_config` | no | `{}` | Stream or multipart local upload |
| `session` | no | owned session | Caller-owned aiohttp session |
| `serialization` | no | internal JSON serializer | Callable returning JSON text |
| `allow_redirects` | no | `True` | Follow owned redirect loop |
| `max_redirects` | no | `10` | Redirect ceiling |
| `allowed_schemes` | no | HTTP/HTTPS | Per-call URL scheme allowlist |
| `cross_origin_headers` | no | safe set | Extra headers allowed across origins |
| `max_response_bytes` | no | `67108864` | Response/read ceiling |
| `circuit_breaker_config` | no | `None` | Breaker and retry configuration |
| `redact_query_params` | no | `None` | Additional sensitive query names |

Upload config supports `local_filepath` plus either
`file_upload_chunk_size` for raw streaming or `file_key` for multipart.
Download config supports `download_filepath`, `file_download_chunk_size`, and
`overwrite`.

### `protocol_info` — FTP

| Key | Required | Default | Purpose |
|---|---:|---|---|
| `port` | no | `21` | Server port |
| `command` | at operation | — | Fixed FTP operation |
| `server_path` | at operation | — | Remote path |
| `client_path` | depends | `None` | Local path |
| `overwrite` | no | `False` | Allow local replacement |
| `max_response_bytes` | no | `67108864` | Download ceiling |
| `verify_ssl` | no | `True` | Verified TLS; false is plaintext legacy mode |
| `tls_mode` | no | legacy mode | `implicit` or `explicit` |
| `certificate` | no | `None` | Implicit-FTPS client certificate |
| `timeout` | no | `15` | Connect/socket-operation seconds |
| `circuit_breaker_config` | no | `None` | Breaker and retry configuration |
| `redact_query_params` | no | `None` | Additional sensitive names |

### `protocol_info` — SFTP

| Key | Required | Default | Purpose |
|---|---:|---|---|
| `port` | no | `22` | SSH port |
| `mode` | at operation | — | `get`, `put`, or `remove` |
| `remote_path` | at operation | — | Remote path |
| `local_path` | depends | `None` | Local path |
| `overwrite` | no | `False` | Allow local replacement |
| `max_response_bytes` | no | `67108864` | Download ceiling |
| `additional_arguments` | no | `{}` | Forwarded asyncssh operation options |
| `known_hosts` | no | asyncssh default | Known-hosts source |
| `host_key` | no | `None` | Explicit host-key pin |
| `client_keys` | no | `None` | SSH client keys |
| `insecure_skip_host_key_check` | no | `False` | Explicit insecure bypass |
| `timeout` | no | `15` | Connect/login seconds |
| `circuit_breaker_config` | no | `None` | Breaker and retry configuration |
| `redact_query_params` | no | `None` | Additional sensitive names |

Authentication is separate from `protocol_info`. `SFTPAuth` carries
`username`, optional `password`, optional `client_keys`, optional
`key_passphrase`, and the opt-in `use_ssh_agent` flag. The table's
`client_keys` key remains as a compatibility bridge for legacy
`.login`/`.password` auth objects.

### `protocol_info` — SOAP

| Key | Required | Default | Purpose |
|---|---:|---|---|
| `soap_version` | no | `1.1` | SOAP `1.1` or `1.2` |
| `soap_action` | no | `None` | SOAP action |
| `soap_headers` | no | `None` | XML Header `Element` |
| `headers` | no | `{}` | Additional HTTP headers |
| `cookies` | no | `None` | HTTP cookies |
| `certificate` | no | `None` | Client certificate/key pair |
| `verify_ssl` | no | `True` | Verify server TLS |
| `trace_config` | no | a built-in tracer | HTTP tracing |
| `timeout` | no | `15` | Whole-exchange seconds |
| `cross_origin_headers` | no | safe set | Redirect forwarding additions |
| `session` | no | owned session | Caller-owned aiohttp session |
| `serialization` | forbidden | — | SOAP refuses custom JSON serialization |
| `max_response_bytes` | no | `67108864` | Response ceiling |
| `allow_redirects` | no | `True` | Follow redirects |
| `max_redirects` | no | `10` | Redirect ceiling |
| `allowed_schemes` | no | HTTP/HTTPS | Scheme allowlist |
| `circuit_breaker_config` | no | `None` | Breaker and retry configuration |
| `redact_query_params` | no | `None` | Additional sensitive query names |

### `protocol_info` — JSONRPC

| Key | Required | Default | Purpose |
|---|---:|---|---|
| `method` | yes | — | Non-empty method not starting `rpc.` |
| `request_id` | yes | — | String or bounded integer correlation ID |
| `headers` | no | `{}` | Additional compatible headers |
| `cookies` | no | None | HTTP cookies |
| `certificate` | no | `None` | Client certificate/key pair |
| `verify_ssl` | no | `True` | Verify server TLS |
| `trace_config` | no | a built-in tracer | HTTP tracing |
| `timeout` | no | `15` | Whole-exchange seconds |
| `max_response_bytes` | no | `67108864` | Response ceiling |
| `circuit_breaker_config` | no | `None` | Breaker and retry configuration |
| `redact_query_params` | no | `None` | Additional sensitive query names |

### `protocol_info` — GRAPHQL

| Key | Required | Default | Purpose |
|---|---:|---|---|
| `query` | yes | — | Non-empty GraphQL document |
| `operation_name` | no | `None` | Named operation |
| `headers` | no | `{}` | Additional compatible headers |
| `cookies` | no | None | HTTP cookies |
| `certificate` | no | `None` | Client certificate/key pair |
| `verify_ssl` | no | `True` | Verify server TLS |
| `trace_config` | no | a built-in tracer | HTTP tracing |
| `timeout` | no | `15` | Whole-exchange seconds |
| `max_response_bytes` | no | `67108864` | Response ceiling |
| `circuit_breaker_config` | no | `None` | Breaker and retry configuration |
| `redact_query_params` | no | `None` | Additional sensitive query names |

### `protocol_info` — S3

| Key | Required | Default | Purpose |
|---|---:|---|---|
| `command` | yes | — | `download`, `upload`, `head`, or `list` |
| `local_path` | by command | — | Download destination or upload source |
| `region` | no | provider default | AWS region |
| `max_response_bytes` | download | `67108864` | Download ceiling |
| `max_upload_bytes` | upload | `67108864` | Upload-source ceiling |
| `max_items` | list | `1000` | One-page limit, 1–1000 |
| `continuation_token` | no | `None` | Continue a list page |
| `circuit_breaker_config` | no | `None` | Breaker and retry configuration |
| `redact_query_params` | no | `None` | Additional sensitive names |

**S3 command option allowlists.**

| Command | Accepted keys |
|---|---|
| `download` | command, local_path, region, max_response_bytes, circuit_breaker_config, redact_query_params |
| `upload` | command, local_path, region, max_upload_bytes, circuit_breaker_config, redact_query_params |
| `head` | command, region, circuit_breaker_config, redact_query_params |
| `list` | command, region, max_items, continuation_token, circuit_breaker_config, redact_query_params |

**S3 success detail schemas.**

| Command | `protocol_details` keys |
|---|---|
| `download` | command, bucket, key, local_path, bytes_written, etag |
| `upload` | command, bucket, key, local_path, bytes_read, etag |
| `head` | command, bucket, key, content_length, content_type, etag, last_modified, metadata |
| `list` | command, bucket, prefix, items, key_count, is_truncated, next_continuation_token |

### `protocol_info` — GRPC

| Key | Required | Default | Purpose |
|---|---:|---|---|
| `method` | yes | — | Canonical `/package.Service/Method` path |
| `metadata` | no | `()` | Ordered ASCII metadata pairs |
| `request_serializer` | no | `None` | Synchronous object-to-bytes callable |
| `response_deserializer` | no | `None` | Synchronous bytes-to-JSON-safe callable |
| `timeout` | no | `15` | RPC deadline seconds |
| `max_response_bytes` | no | `67108864` | Receive ceiling, at most `2^31-1` |
| `circuit_breaker_config` | no | `None` | Breaker and retry configuration |
| `redact_query_params` | no | `None` | Additional sensitive names |

**gRPC status map.**

| gRPC status | `status_code` |
|---|---:|
| `CANCELLED` | 499 |
| `UNKNOWN` | 502 |
| `INVALID_ARGUMENT` | 400 |
| `DEADLINE_EXCEEDED` | 504 |
| `NOT_FOUND` | 404 |
| `ALREADY_EXISTS` | 409 |
| `PERMISSION_DENIED` | 403 |
| `RESOURCE_EXHAUSTED` | 429 |
| `FAILED_PRECONDITION` | 412 |
| `ABORTED` | 409 |
| `OUT_OF_RANGE` | 400 |
| `UNIMPLEMENTED` | 501 |
| `INTERNAL` | 500 |
| `UNAVAILABLE` | 503 |
| `DATA_LOSS` | 500 |
| `UNAUTHENTICATED` | 401 |

### Rejected capabilities

The closed selectors intentionally reject escape hatches that would make their
wire contract ambiguous:

- JSON-RPC and GraphQL reject caller `request_type`, HTTP `file transfer`,
  caller-owned `session`, custom JSON `serializer`, `redirect` controls,
  `allowed-scheme` overrides, `port override`, and `cross-origin` forwarding.
- S3 rejects `endpoint override`, `arbitrary SDK` calls, delete, copy,
  presigning, session-token/profile controls, and automatic pagination.
- gRPC rejects arbitrary `channel option`, `compression`, library-level
  `credentials`, `custom roots`, reflection, streaming, and alternate
  `method-shape` syntax.

## The response envelope

Use `ok`, not the numeric status alone, as the success predicate.

| Key | Meaning |
|---|---|
| `ok` | Semantic success |
| `status_code` | Real or normalized HTTP-shaped status |
| `protocol` | Canonical selector |
| `url` | Redacted target |
| `request_time` | ISO-8601 UTC start time |
| `latency` | Monotonic elapsed seconds |
| `payload` | Redacted caller payload echo |
| `text` | Decoded body or base64 gRPC response |
| `json` | Parsed JSON or deserialized gRPC value |
| `headers` | Redacted response headers |
| `cookies` | Redacted response cookies |
| `error` | `GatewayError` or `None` |
| `protocol_details` | Protocol-specific normalized details |
| `request_tracer` | HTTP-family trace events |
| `pre_processor_response` | Preprocessor return value |
| `post_processor_response` | Postprocessor return value |

`error`, when present, is a `GatewayError` with this exact shape:

| Key | Meaning |
|---|---|
| `type` | Exception class name |
| `code` | Wire-stable error code |
| `message` | Redacted human-readable message |
| `cause` | Redacted normalized cause |

### `status_code` per protocol

| Situation | Result |
|---|---|
| `HTTP` / `HTTPS` remote response | Real HTTP status |
| `SOAP` response or Fault | Real HTTP status |
| `JSONRPC` result or application error | Real HTTP status, including 200 errors |
| `GRAPHQL` data or errors | Real HTTP status, including 2xx errors |
| Successful `FTP` / `SFTP` / `GRPC` operation | 200 |
| Successful `S3` operation | Valid SDK 2xx status; 200 if metadata omits it |
| `SFTP` no-such-file / permission denied | 404 / 403 |
| `GRPC` failure | Canonical mapping in the gRPC table |
| Local or transport failure | Stable mapping in the error-code table |

### The error-code table

| code | Default status | Meaning |
|---|---:|---|
| `GATEWAY` | 502 | Base gateway failure |
| `TRANSPORT` | 502 | Base transport failure |
| `PROTOCOL` | 502 | Base protocol failure |
| `CONFIG` | 400 | Invalid configuration |
| `PROCESSOR` | 500 | Caller processor failed |
| `SERIALIZATION` | 502 | Decode/encode failure; request-side may be 400 |
| `XML_UNSAFE` | 502 | Unsafe XML response |
| `PATH` | 400 | Unsafe path |
| `CONNECT` | 502 | Connection failed |
| `DNS` | 502 | Name resolution failed |
| `TLS` | 502 | TLS negotiation or verification failed |
| `HOST_KEY` | 495 | SSH host-key verification failed |
| `TIMEOUT` | 504 | Deadline exceeded |
| `RESPONSE_TOO_LARGE` | 502 | Byte ceiling exceeded |
| `RESPONSE_TOO_DEEP` | 502 | Structure depth ceiling exceeded |
| `CIRCUIT_OPEN` | 503 | Circuit currently refuses work |
| `STACK_EXHAUSTED` | 502 | Recursion limit reached |
| `HTTP_STATUS` | 502 | HTTP remote failure; remote status overrides |
| `JSONRPC_ERROR` | 502 | JSON-RPC application error; remote status overrides |
| `JSONRPC_PROTOCOL` | 502 | Malformed JSON-RPC peer response |
| `GRAPHQL_ERROR` | 502 | GraphQL application errors; remote status overrides |
| `GRAPHQL_PROTOCOL` | 502 | Malformed GraphQL peer response |
| `S3_STATUS` | 502 | S3 service error; mapped remote status overrides |
| `GRPC_STATUS` | 502 | gRPC status; canonical mapping overrides |
| `FTP_STATUS` | 500 | FTP reply failure; reply status overrides |
| `SFTP_STATUS` | 500 | SFTP status; mapped status overrides |
| `SOAP_FAULT` | 502 | SOAP Fault; HTTP status overrides |

The public hierarchy is `AsyncGatewayError`, `ConfigurationError`,
`UnsupportedVerbError`, `ProcessorError`, `SerializationError`,
`UnsafeXmlError`, `PathContainmentError`, `LocalWriteError`, `TransportError`,
`ConnectError`, `DnsError`, `TlsError`, `HostKeyError`,
`GatewayTimeoutError`, `ResponseTooLargeError`, `ResponseTooDeepError`,
`CircuitOpenError`, `StackExhaustedError`, `ProtocolError`,
`HttpStatusError`, `JsonRpcError`, `JsonRpcProtocolError`, `GraphqlError`,
`GraphqlProtocolError`, `S3StatusError`, `GrpcStatusError`, `FtpStatusError`,
`SftpStatusError`, and `SoapFaultError`.

### What raises and what returns

Boundary mistakes raise `ConfigurationError`; protocol and remote failures
normally return `ok=False`. Unexpected implementation exceptions and
cancellation propagate.

The errors that escape this way are: selector and processor-shape failures;
unknown top-level keywords; non-string or security-like unknown legacy
options; unknown keys and missing required keys for `JSONRPC`, `GRAPHQL`,
`S3`, and `GRPC`; invalid URL, auth, payload, method, metadata, or command
values found before dispatch; and HTTP/SOAP constructor validation. The four
deferred FTP/SFTP option checks run inside their operation and therefore return
a `CONFIG`/400 envelope. S3 credential-provider discovery can happen only
during the operation and likewise returns a `CONFIG`/400 envelope. Robust
callers both catch `ConfigurationError` and inspect `result['ok']`.

## Reliability

Every destination receives a process-lifetime circuit breaker. The default
breaker opens after five consecutive counted failures, stays open for 60
seconds, and admits one half-open trial. Retries are off unless a retry config
is supplied.

`circuit_breaker_config` keys — an unknown key is rejected:

| Key | Purpose |
|---|---|
| `maximum_failures` | Failures before opening |
| `timeout` | Open-state seconds |
| `retry_config` | Nested retry policy |
| `clock` | Injectable monotonic clock |
| `sleep` | Injectable async sleep |

`retry_config` keys — likewise closed:

| Key | Purpose |
|---|---|
| `name` | Diagnostic label |
| `allowed_retries` | Required retries after the first attempt |
| `retriable_exceptions` | Explicit retryable classes |
| `abortable_exceptions` | Immediate-abort classes |
| `on_retries_exhausted` | Synchronous callback |
| `on_failed_attempt` | Synchronous callback |
| `on_abort` | Synchronous callback |
| `delay` | Base delay seconds |
| `max_delay` | Delay ceiling |
| `jitter` | Randomize backoff |
| `backoff` | `constant` or `exponential` |

### New selector retry rules

JSONRPC and GRAPHQL inherit transport retries, so retrying a POST may cause
duplicate delivery; use idempotency at the application layer. In
`retry_config`, `allowed_retries=N` means `N+1` total attempts.

S3 sets the SDK to `total_max_attempts=1`; SDK retries are disabled so the
gateway owns replay and breaker accounting. gRPC retries only `UNKNOWN`,
`DEADLINE_EXCEEDED`, `INTERNAL`, and `UNAVAILABLE`. `RESOURCE_EXHAUSTED` is
abortable and uncounted, including a local receive-cap rejection.

### Timeouts

For HTTP-family protocols, one timeout bounds the whole exchange, redirect
chain included. FTP/SFTP timeouts bound connection and individual transport
operations rather than the full transfer. gRPC uses the value as the RPC
deadline.

## Security boundaries

### S3 and gRPC security boundaries

With `auth=None`, S3 uses the normal AWS credential chain. A supplied auth
object contributes an access key and secret access key through `login` and
`password`; credential-provider diagnostics are redacted. Caller continuation
token values are redacted on failure, while a successful server-issued next
token remains usable.

gRPC requires `auth=None`. Use `grpc://host:port` for deliberate plaintext or
`grpcs://host:port` for TLS with platform roots. Only unary-unary methods are
supported. Request metadata is limited to 64 pairs, 8192 printable ASCII
characters per value, and 32768 aggregate value characters. Sensitive peer
metadata is masked; binary peer metadata is bounded and represented as base64.

### Rejected downgrade paths

TLS verification defaults on. Disabling HTTP verification warns; disabling
FTP verification without a named mode selects legacy plaintext FTP and warns.
Explicit FTPS upgrades before login. SFTP host verification can only be
disabled by the named insecure option, and SSH-agent discovery is disabled
unless `SFTPAuth(use_ssh_agent=True)` opts in.

## Redaction and the payload echo

The envelope echoes caller data in `payload`. Mapping values under recognized
sensitive keys are masked by key name to a depth of 4. Below depth 4, arbitrary
values may remain visible. If the payload is not a mapping, it is echoed
verbatim. Do not place secrets under novel key names and assume the library can
infer their meaning.

URLs, userinfo, sensitive query parameters, headers, cookies, error messages,
causes, and rendered tracebacks pass through the shared redaction layer.
`redact_query_params` can widen the built-in query-name set.

## You own URL validation

The library validates protocol shape and scheme; it is not an SSRF policy.
Validate untrusted destinations against your own host, network, and DNS rules
before calling. For HTTP-family calls, narrow `allowed_schemes` when your use
case permits it, and never treat that scheme list as a host allowlist.

## Local files

Downloads refuse to overwrite by default; opt in only with `overwrite=True`
where that option exists. New files use exclusive `O_EXCL` creation, guarded
walks use `O_NOFOLLOW`, and temporary files use mode `0600`. Each symbolic link
and path-substitution attack is checked through held descriptors. Relative
paths are contained
under the process working directory; use intentional absolute destinations
when application policy allows them.

S3 selector downloads never overwrite. The compatibility S3 helper retains
its historical overwrite default, which is why its call site deserves an
explicit policy choice.

## Logging

One failed request produces one structured log record with the protocol,
redacted URL, stable code, status, latency, and a pre-rendered redacted
traceback in `extra['traceback']`. The logger does not emit `exc_info=True`;
that prevents an unredacted native traceback from bypassing the redactor, but
some APM tools cannot group failures by native exception identity. If you
change that trade-off in an application adapter, apply equivalent redaction
before restoring native exception grouping.

## Operations and project workflow

### Examples

The complete scripts live in the
[repository examples directory](https://github.com/ajyadav013/asyncio-gateway/tree/master/examples).
The `examples/` directory is in the repository, not in the package; it ships in
neither the wheel nor the sdist.

- `http_example.py`
- `ftp_example.py`
- `sftp_example.py`
- `soap_example.py`
- `error_handling_example.py`
- `jsonrpc_example.py`
- `graphql_example.py`
- `s3_example.py`
- `grpc_example.py`

### Docker: what the test stack covers

The Docker/Compose test stack exercises live HTTP/HTTPS, FTPS, SFTP, and SOAP
services. JSON-RPC and GraphQL tests use loopback HTTP endpoints. S3 uses a
deterministic SDK double and does not contact AWS. gRPC uses a local generic
server. The stack therefore validates each public path without pretending to
provide external cloud infrastructure.

### Development checks

```text
python -m pip install -e '.[dev]'
pytest
flake8 .
mypy asyncio_gateway
```

The suite mechanically executes README examples, compares tables with source
allowlists, checks 100% line and branch coverage, builds both artifacts, and
smokes the installed public registry.

### Cutting a release

Change the version in exactly one file: `pyproject.toml`. Date the matching
CHANGELOG heading, merge through CI, and let `publish.yml` build, verify,
publish, tag, and create the GitHub release for the exact green commit. A
version whose changelog still says unreleased is intentionally blocked.

## Migrating from `asyncio-requests`

The predecessor distribution stopped at `asyncio-requests` 2.7.3. Migration is
explicit: install `asyncio-gateway`, change imports from `asyncio_requests` to
`asyncio_gateway`, then update call sites to the common envelope and stricter
security defaults. Read the [security advisory](CHANGELOG.md#security-advisory--asyncio-requests--273)
for the defects fixed during the rename.

## Licence and attribution

Licensed under the MIT License. Copyright 2026 Arjunsingh Yadav; the project
also retains its 2022 Fynd attribution in `LICENSE`.
