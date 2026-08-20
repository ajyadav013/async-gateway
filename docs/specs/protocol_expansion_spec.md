# Specification: Protocol Expansion

## Goal

Extend the existing unary `request(...) -> GatewayResponse` library contract
with JSON-RPC 2.0, GraphQL, S3, and unary gRPC strategies and with explicit
FTPS support. Preserve all existing protocol behavior, especially FTP's
current implicit-TLS and opt-in plaintext semantics, while applying the same
envelope, circuit-breaker, redaction, validation, and typed-error rules as the
existing strategies.

## Assumptions and boundaries

- `REST` remains ordinary HTTP/HTTPS usage, not a selector.
- All five additions are one-shot operations that finish in one
  `GatewayResponse`; streaming and connection-lifetime APIs remain out of
  scope.
- Existing dependencies are sufficient for FTPS, JSON-RPC, GraphQL, and S3.
  Unary gRPC adds the official `grpcio>=1.83.0,<2` runtime, verified on PyPI
  with Python 3.10–3.14 support. Generic byte serialization avoids a protobuf
  or code-generator dependency.
- S3 tests use injected/mocked SDK sessions and clients. They never consult
  real AWS configuration, metadata services, credentials, or buckets.
- GraphQL is a narrow JSON-over-HTTP semantic adapter. It does not parse or
  validate GraphQL documents and does not introduce Strawberry/Apollo.
- gRPC is unary-unary only. Streaming needs a separate connection/iterator API
  and remains out of this materialized-envelope contract.
- Additive selector/configuration support does not require a version bump in
  this implementation branch.

## Requirements and acceptance criteria

### R1 — Public registration and common contract

`resolve_protocol()` shall accept case-insensitive `JSONRPC`, `GRAPHQL`, `S3`,
and `GRPC` selectors. All strategies shall use the existing response skeleton,
one typed-error conversion point, per-destination breaker, redaction policy,
and processor lifecycle.

- **AC1.1:** HTTP, HTTPS, FTP, SFTP, and SOAP calls remain behaviorally
  unchanged when new options/selectors are not used.
- **AC1.2:** All registered protocols appear in contract fixtures, hostile
  input invariants, envelope tests, documentation inventories, and examples.
- **AC1.3:** Expected remote/transport failures become `ok=False` envelopes;
  caller configuration errors rejected at the public boundary still escape as
  typed `ConfigurationError`; programming defects propagate. Cancellation
  propagates except after AC6.6's atomic-overwrite commit point, where the
  already-started local commit is completed and reported honestly as success.
- **AC1.4:** No transport credential, Authorization value, or configured
  sensitive query/metadata value introduced by these strategies appears in
  returned envelopes, messages, tracebacks, or logs; caller payloads retain
  the existing documented bounded-redaction contract.
- **AC1.5:** The dispatcher preserves `data is None` in the initial envelope
  only for `JSONRPC` and `GRAPHQL`, so those adapters can distinguish an
  omitted params/variables member from an empty object. Every existing
  selector retains the legacy `None -> {}` normalization. A pre-processor may
  still deliberately replace the new selectors' payload before dispatch.

### R2 — Explicit FTPS without legacy drift

The existing `FTP` strategy shall accept optional
`protocol_info['tls_mode']`, whose only values are `implicit` and `explicit`.

- **AC2.1:** When `tls_mode` is absent, behavior is byte-for-byte compatible:
  `verify_ssl=True` uses verified implicit TLS and `verify_ssl=False` uses
  plaintext with the existing warning.
- **AC2.2:** Named `implicit` mode uses the existing verified implicit TLS
  path. Named `explicit` mode opens plaintext control transport, invokes
  `AUTH TLS` before login through `aioftp`'s native
  `upgrade_to_tls(prebuilt_verified_context)`, then protects data with `PBSZ 0`
  and `PROT P`. Building platform trust is offloaded before connection; aioftp
  never builds its default context on the event-loop path.
- **AC2.3:** A named TLS mode combined with `verify_ssl=False` raises
  `ConfigurationError` before any connection; there is no unverified or
  silently downgraded named TLS mode.
- **AC2.4:** Explicit mode combined with a client `certificate` raises
  `ConfigurationError` before connection. Explicit-mode mTLS is outside this
  release's frozen public authentication surface; named implicit mode retains
  the existing client-certificate path.
- **AC2.5:** Invalid `tls_mode` shapes/values fail before connection. Existing
  port selection remains unchanged and caller-overridable.
- **AC2.6:** `protocol_details['tls_mode']` reports the effective value
  (`implicit`, `explicit`, or legacy `plaintext`) without exposing secrets.

### R3 — JSON-RPC 2.0 request contract

`protocol='JSONRPC'` shall perform one logical HTTP(S) call. Every transport
attempt is a POST. Required
`protocol_info` keys are a non-empty string `method` and `request_id`, which
must be a string or integer but not a boolean. Integer identifiers are limited
to the repository JSON engine's exactly round-trippable wire domain,
`-2^63..2^64-1` inclusive; larger identifiers use strings. `data` is the RPC
`params` and must be a mapping, a list, or `None`.

- **AC3.1:** Only `http` and `https` targets dispatch. JSON-RPC explicitly
  rejects URL user info, and the dispatcher rejects protocol-relative,
  malformed, and non-HTTP URLs before network I/O.
- **AC3.2:** The wire payload is `jsonrpc='2.0'`, the exact method and id, and
  `params` only when `data` is not `None`; `Content-Type` is JSON and callers
  cannot override it with a conflicting value.
- **AC3.3:** Batch inputs, notifications (missing/null ids), empty method
  names or names beginning with the reserved `rpc.` prefix, and scalar/byte
  params are rejected before dispatch.
- **AC3.4:** HTTP transport, timeouts, TLS, redirect guards, auth, trace
  collection, retry, byte caps, and response preservation reuse the proven
  HTTP path, but
  redirects are disabled for this selector so a POST cannot be rewritten or
  replayed at another target. The adapter always creates and closes its own
  `aiohttp.ClientSession` with `raise_for_status=False` and the library's JSON
  serializer; caller-supplied sessions and serializers are refused, so session
  defaults cannot preempt body copying or alter the exact RPC envelope. With
  no explicit retry policy there is one attempt. If the caller configures
  `allowed_retries=N`, the same POST may be attempted at most `N+1` times after
  retryable transport failures; this duplicate-delivery risk is documented
  for non-idempotent RPC methods.

### R4 — JSON-RPC 2.0 response semantics

A successful response must be a JSON object with `jsonrpc == '2.0'`, an id of
the same scalar type and value as the request id, and exactly one of `result`
or `error`.

- **AC4.1:** A result response returns `ok=True`; `protocol_details` includes
  `id` and `result`, including a legitimate null result.
- **AC4.2:** An error object requires an integer non-boolean `code` in the
  same exactly round-trippable `-2^63..2^64-1` JSON wire domain and a string
  `message`; optional `data` is preserved. It raises a typed
  `JsonRpcError` with stable code `JSONRPC_ERROR`. A valid RPC error at HTTP
  200 still yields `ok=False` while retaining status, headers, text, parsed
  JSON, and protocol details.
- **AC4.3:** Wrong version/id, both or neither result/error, a malformed error,
  non-object JSON, or undecodable JSON at a successful HTTP status raises
  `JsonRpcProtocolError` with stable code `JSONRPC_PROTOCOL` and status 502,
  while retaining every response field already read.
- **AC4.4:** A valid JSON-RPC error takes precedence over HTTP failure status;
  an HTTP failure without a valid JSON-RPC error remains `HTTP_STATUS`.
- **AC4.5:** JSON-RPC and GraphQL share the existing HTTP request/session,
  redirect guard, body-cap, trace, redaction, and envelope-copy machinery.
  `HttpRequest` gains one post-copy response-error seam: its default preserves
  the current HTTP status-before-body ordering, while semantic subclasses may
  inspect the already-populated envelope and choose their documented
  application-error precedence. They do not copy `_exchange()` or the
  transport helper.

### R5 — S3 target, credentials, and allowlist

`protocol='S3'` shall accept `s3://bucket[/key-or-prefix]` and require
`protocol_info['command']` in `download`, `upload`, `head`, or `list`.

- **AC5.1:** The URI requires a non-empty bucket and rejects user info, ports,
  query strings, fragments, wrong schemes, and ambiguous authorities before
  creating an SDK session. The object key/prefix is the path without its one
  leading slash; it is not treated as a local filesystem path.
- **AC5.2:** `auth=None` uses the normal AWS credential chain. Supplied auth
  must expose non-empty string `.login` and `.password`, passed only as access
  key id and secret access key. No new auth type or explicit session-token
  field is introduced.
- **AC5.3:** Optional `region` is a non-empty string. Arbitrary SDK method
  names, endpoint overrides, delete, and multi-page implicit iteration are
  not accepted.
- **AC5.4:** The breaker destination is bucket-scoped under the S3 family;
  one bucket's failures do not open another bucket's circuit.

### R6 — S3 operation behavior

- **AC6.1 Download:** requires a non-empty key and `local_path`; streams
  `get_object` body chunks through the guarded non-overwriting writer; refuses
  an advertised or observed size above validated `max_response_bytes`; closes
  the body on success, refusal, error, and cancellation; records bytes written.
- **AC6.2 Upload:** requires a non-empty key and `local_path`; opens the source
  once with `O_RDONLY|O_NOFOLLOW|O_CLOEXEC` where available, holds that
  descriptor through the read, and uses `fstat` to require a readable regular
  file. Where `O_NOFOLLOW` is unavailable, a pre-open `lstat` must identify a
  regular file and its device/inode must match the held descriptor's `fstat`;
  a mismatch is refused. It validates a positive `max_upload_bytes`
  (defaulting to the existing `MAX_RESPONSE_BYTES` value), reads asynchronous
  chunks from that same held
  descriptor with an observed-byte cap, and passes the resulting bounded byte
  body to `put_object`; the SDK never reopens the caller's pathname. Success,
  failure, and cancellation close the descriptor. The envelope records the
  observed byte count and returned ETag when available.
- **AC6.3 Head:** requires a non-empty key and normalizes safe metadata into
  the envelope (`content_length`, `content_type`, `etag`, ISO-formatted
  `last_modified`, and string metadata) without returning SDK objects.
- **AC6.4 List:** treats the URI path as a prefix, requests one page with
  validated `max_items` in `1..1000` (default 1000), accepts an optional
  non-empty continuation token, and returns normalized items plus
  `is_truncated` and `next_continuation_token`. It never follows another page
  implicitly.
- **AC6.5:** The existing public `download_file_from_s3()` and the S3 strategy
  share one hardened download primitive; two independent transfer
  implementations are not permitted. Compatibility is explicit: the public
  helper keeps its keyword-only signature, `None` return, overwrite-by-default
  behavior, and uncapped default. New optional `overwrite` and
  `max_response_bytes` keyword controls route through the shared primitive;
  the S3 strategy selects `overwrite=False` and a mandatory positive cap.
- **AC6.6:** The shared primitive's overwrite path is failure-atomic. It writes
  to a guarded, uniquely created temporary file in the destination directory,
  closes and flushes the completed file, then atomically replaces the target.
  Any download, cap, cancellation, close, or replace failure removes only the
  temporary file and leaves a pre-existing target byte-for-byte unchanged.
  The non-overwrite path retains exclusive-create behavior and never replaces
  an existing target. The linearization point is dispatch of the atomic
  replace: cancellation observed before it propagates and preserves the old
  target; once dispatch begins, the off-loop replace future is shielded and
  awaited to completion. A cancellation arriving during or after that commit
  is deferred, the committed operation returns success, and no caller is told
  the operation was cancelled after the target may already have changed.

### R7 — S3 failures and response envelope

- **AC7.1:** Botocore service failures preserve the real HTTP status, AWS
  error code/message, request id, and safe response metadata, then raise
  `S3StatusError` with wire-stable code `S3_STATUS`.
- **AC7.2:** Missing/partial credentials become typed configuration failures;
  endpoint, DNS, timeout, TLS, and connection errors map into the existing
  transport vocabulary and breaker classification.
- **AC7.3:** Local path/size/configuration failures abort without incrementing
  the circuit breaker. For SDK service errors, only HTTP 408, 429, 500, 502,
  503, or 504, or AWS codes `RequestTimeout`, `RequestTimeoutException`,
  `Throttling`, `ThrottlingException`, `SlowDown`, `InternalError`, or
  `ServiceUnavailable`, are retryable and counted. Every other service status
  or code, including `AccessDenied`, `NoSuchBucket`, `NoSuchKey`, region
  redirects, and conflicts, aborts uncounted. Private retryable/abortable
  service-error types retain the original SDK response through the breaker and
  are both converted afterward to public `S3_STATUS`; classification never
  depends on message text.
- **AC7.4:** Access keys, secret keys, and credential-provider details are
  redacted from all response and logging surfaces. A caller-supplied
  continuation token is not logged. The server's next token is an explicitly
  allowed opaque success field named `next_continuation_token`; it remains in
  `protocol_details` because it is required to request the next explicit page,
  but is never interpolated into errors, tracebacks, or logs.
- **AC7.5:** Every successful S3 operation reports the SDK's valid 2xx
  `ResponseMetadata.HTTPStatusCode`, defaulting to 200 when a conforming test
  double omits response metadata. Exact `protocol_details` schemas are:
  download `{command, bucket, key, local_path, bytes_written, etag}`; upload
  `{command, bucket, key, local_path, bytes_read, etag}`; head
  `{command, bucket, key, content_length, content_type, etag, last_modified,
  metadata}`; and list `{command, bucket, prefix, items, key_count,
  is_truncated, next_continuation_token}`. Optional scalar fields normalize to
  `None`, metadata maps string keys to strings, byte counts are non-negative
  integers, and timestamps are ISO-8601 strings.
- **AC7.6:** A list item is exactly `{key, size, etag, last_modified,
  storage_class}`. `key` is a non-empty string, `size` is a non-negative
  integer, the remaining values are strings or `None`, and service order is
  preserved. `key_count == len(items)`. Malformed SDK success metadata becomes
  `S3_STATUS`/502 rather than leaking an SDK object or silently dropping an
  item.
- **AC7.7:** The aioboto3 client is created with botocore
  `total_max_attempts=1`, disabling SDK-layer replay. The gateway breaker is
  the sole retry owner: absent explicit `retry_config` it makes one SDK
  attempt; `allowed_retries=N` makes at most `N+1` SDK attempts. Tests assert
  both the client configuration and total call count so nested retry
  amplification cannot return unnoticed.

### R8 — GraphQL-over-HTTP request contract

`protocol='GRAPHQL'` shall perform one logical JSON-over-HTTP(S) call. Every
transport attempt is a POST. Required
`protocol_info['query']` is a non-empty GraphQL document string. Optional
`operation_name` is a non-empty string. `data` supplies `variables` and must
be a mapping or `None`.

- **AC8.1:** Only `http` and `https` targets dispatch; malformed,
  protocol-relative, non-HTTP, and user-info-bearing URLs fail before I/O.
- **AC8.2:** The body contains exactly `query`, optional `operationName`, and
  optional `variables`. The request uses `Content-Type: application/json` and
  advertises `application/graphql-response+json` plus legacy
  `application/json`; conflicting caller media-type headers are refused.
- **AC8.3:** Query parsing, schema validation, code generation, GET, file
  upload, subscriptions, and incremental/multipart responses are out of
  scope. HTTP/TLS/auth/timeout/trace/retry/cap behavior reuses the existing
  HTTP path. Redirects are disabled, and the adapter owns its
  `raise_for_status=False` session and JSON serializer; caller sessions and
  serializers are refused. With no explicit retry policy there is one attempt;
  `allowed_retries=N` permits at most `N+1` identical POST attempts after
  retryable transport failures and is documented as a possible duplicate
  mutation delivery.

### R9 — GraphQL response semantics

A GraphQL response shall be a JSON object containing `data`, `errors`, or
both. If present, `errors` must be a non-empty list of objects with a
non-empty string `message`; safe `path`, `locations`, and `extensions` values
are preserved.

- **AC9.1:** `data` without errors returns `ok=True` and is retained in both
  parsed response material and protocol details, including legitimate nulls
  only when accompanied by errors.
- **AC9.2:** Any non-empty `errors` list raises `GraphqlError` with stable code
  `GRAPHQL_ERROR`, even at HTTP 2xx. Partial `data` remains available; the
  error list, HTTP status, headers, cookies, text, and parsed body survive.
- **AC9.3:** Non-object JSON, neither key, empty/malformed errors, or an
  impossible null-data-without-errors response raises `GraphqlProtocolError`
  with stable code `GRAPHQL_PROTOCOL` and default status 502.
- **AC9.4:** A valid GraphQL error body declared as
  `application/graphql-response+json` takes precedence over HTTP status. A
  non-2xx `application/json`, missing/other content type, malformed GraphQL
  response, or valid data-without-errors remains `HTTP_STATUS`; legacy
  `application/json` bodies are trusted for GraphQL semantics only at 2xx.

### R10 — Unary gRPC request and channel contract

`protocol='GRPC'` shall support only unary-unary calls using `grpc.aio`.
Targets are `grpc://host:port` (explicit plaintext) or
`grpcs://host:port` (TLS with platform roots); host and numeric port are
required and user info, path, query, and fragment are rejected.

- **AC10.1:** Required `protocol_info['method']` matches
  `/package.Service/Method`. Streaming methods, reflection, generated stubs,
  compression, endpoint options, custom roots, and mTLS are out of scope.
- **AC10.2:** `auth` must be `None`. Optional request metadata is a sequence of
  at most 64 `(key, value)` pairs, preserving order and duplicates. Each key is
  a 1..64-character lowercase ASCII string matching `[0-9a-z_.-]+`; each value
  is a string of at most 8192 printable ASCII characters. The aggregate value
  length is at most 32768 characters. Reserved `grpc-` keys and binary `-bin`
  metadata are refused. Sensitive metadata values are never echoed and use
  existing redaction rules in failures/logs.
- **AC10.3:** Without `request_serializer`, `data` must be bytes-like. An
  optional synchronous serializer receives `data` and must return bytes.
  Serialization/configuration failures occur before channel/RPC creation and
  do not increment the breaker.
- **AC10.4:** The strategy creates one async channel per call, uses
  `secure_channel` only for `grpcs`, awaits one unary result with the validated
  deadline/metadata, applies the validated positive `max_response_bytes`
  (default `MAX_RESPONSE_BYTES`) as its receive-message ceiling, and closes
  the channel on success, failure, and cancellation. There is no scheme
  downgrade or hidden insecure default.
- **AC10.5:** The raw response is bytes. An optional synchronous
  `response_deserializer` may normalize it for `json`. On every received
  response, `text` is the raw bytes encoded as base64 ASCII and
  `protocol_details['response_encoding'] == 'base64'`; without a deserializer
  `json` is `None`, and with one `json` is exactly its validated result. A
  deserializer result must be finite JSON-safe data (`None`, bool, integer,
  finite float, string, lists, or string-keyed mappings recursively); any
  other result or deserializer failure is `SERIALIZATION`/502 after response
  receipt, with the base64 `text` and response metadata retained.

### R11 — gRPC status and envelope semantics

- **AC11.1:** Success reports `ok=True`, status 200, and this exact
  `protocol_details` shape: `{method, grpc_status, grpc_details,
  response_encoding, initial_metadata, trailing_metadata,
  initial_metadata_omitted, trailing_metadata_omitted}`. `grpc_status` is
  `'OK'`, `grpc_details` is `None`, and `response_encoding` is `'base64'`.
- **AC11.2:** `grpc.aio.AioRpcError` raises `GrpcStatusError` with stable code
  `GRPC_STATUS`; protocol details retain the canonical status name, safe
  details, and normalized initial/trailing metadata in the same exact field
  set as success. Failure uses `response_encoding=None`, `text=''`, and
  `json=None`; `grpc_details` is a redacted string or `None`. Each of
  `initial_metadata` and `trailing_metadata` is independently an ordered list
  of at most 64 `{key, value, encoding}` mappings: sensitive values are `***`,
  ASCII values use `encoding=None`, and peer `-bin` byte values are base64
  with `encoding='base64'`. Absent metadata is `[]`; the corresponding
  `initial_metadata_omitted` or `trailing_metadata_omitted` is zero, otherwise
  it counts only excess entries from that collection. Debug strings are not
  exposed.
- **AC11.3:** Status mapping is deterministic: CANCELLED 499, UNKNOWN 502,
  INVALID_ARGUMENT/OUT_OF_RANGE 400, DEADLINE_EXCEEDED 504, NOT_FOUND 404,
  ALREADY_EXISTS/ABORTED 409, PERMISSION_DENIED 403, RESOURCE_EXHAUSTED 429,
  FAILED_PRECONDITION 412, UNIMPLEMENTED 501, INTERNAL/DATA_LOSS 500,
  UNAVAILABLE 503, and UNAUTHENTICATED 401.
- **AC11.4:** Only transport-like statuses (`UNKNOWN`, `DEADLINE_EXCEEDED`,
  `INTERNAL`, and `UNAVAILABLE`) count toward retry and breaker failure policy;
  every other status aborts retries. `RESOURCE_EXHAUSTED` is always abortable
  and uncounted: grpcio uses it both for peer-side quota failures and for the
  local receive-message ceiling, and there is no stable structural field that
  distinguishes those cases without parsing implementation-specific message
  text. The conservative rule prevents a deterministic oversized response
  from being replayed or opening the destination circuit. Task cancellation
  calls `cancel()` and propagates `CancelledError` unchanged.
- **AC11.5:** Inside the breaker callable, a caught `AioRpcError` is converted
  to one of two private typed failures that both retain the original status:
  retryable transport status or abortable application status. The former is
  counted/retried under the existing policy; the latter is added to the
  helper's abortable set and is never counted. Both are converted after the
  breaker seam to the same public `GrpcStatusError`, so internal control flow
  does not alter the stable envelope code or status mapping.

### R12 — Selector option inventories

The four new selectors reject every unknown `protocol_info` key at the public
boundary, before processors or I/O. Existing selectors retain their current
permissive behavior. The accepted keys are frozen as follows; operation-only
keys on the wrong S3 command are also rejected.

| Selector | Required keys | Optional keys |
|---|---|---|
| `JSONRPC` | `method`, `request_id` | `headers`, `cookies`, `certificate`, `verify_ssl`, `trace_config`, `timeout`, `max_response_bytes`, `circuit_breaker_config`, `redact_query_params` |
| `GRAPHQL` | `query` | `operation_name`, `headers`, `cookies`, `certificate`, `verify_ssl`, `trace_config`, `timeout`, `max_response_bytes`, `circuit_breaker_config`, `redact_query_params` |
| `S3` | `command`; `local_path` for download/upload | `region`, download-only `max_response_bytes`, upload-only `max_upload_bytes`, list-only `max_items` and `continuation_token`, `circuit_breaker_config`, `redact_query_params` |
| `GRPC` | `method` | `metadata`, `request_serializer`, `response_deserializer`, `timeout`, `max_response_bytes`, `circuit_breaker_config`, `redact_query_params` |

For JSON-RPC and GraphQL, `request_type`, file-transfer configuration,
`session`, serialization, redirect options, allowed-scheme overrides, endpoint
ports, and cross-origin forwarding are deliberately unsupported. S3 rejects
endpoint overrides and arbitrary SDK options. gRPC rejects channel options,
compression, credentials, roots, reflection, and method-shape switches.

### R13 — Tests and quality

- **AC13.1:** Each lane first adds focused tests that fail for the missing
  behavior (RED), captures that evidence, then implements only enough to pass.
- **AC13.2:** Tests cover happy, empty/boundary, malformed, transport/service,
  retry/breaker, cancellation, redaction, no-I/O-before-validation, and legacy
  compatibility paths. S3 uses deterministic SDK doubles; gRPC uses a local
  generic-bytes server and channel doubles only. Focused gRPC tests prove both
  receive-limit-shaped and peer-originated `RESOURCE_EXHAUSTED` failures abort
  without retrying or incrementing the breaker.
- **AC13.3:** Focused HTTP-semantic tests use hostile caller-session options to
  prove sessions/serializers are refused before I/O. Focused S3 tests prove an
  interrupted overwrite preserves the old target, access-denied failures are
  uncounted, allowlisted transient failures are counted, and explicit gateway
  retries produce exactly `N+1` SDK calls with botocore retries disabled.
  Deterministic cancellation tests cover before, during, and after atomic
  replacement and prove the AC6.6 linearization rule.
- **AC13.4:** Full `pytest` passes with 100% line and branch coverage; no new
  blocking file/network I/O appears on the event-loop path. In particular,
  explicit FTPS builds platform trust off-loop and injects the resulting
  context into `upgrade_to_tls(context)` before login.
- **AC13.5:** `mypy asyncio_gateway`, committed-tree flake8, and exact
  `flake8 .` in a clean worktree pass with no output/errors.
- **AC13.6:** README examples are runnable and documentation/tests cannot
  drift from the registry or accepted-option inventories.

---

# Developer Documentation: Protocol Expansion

## Architecture

The public dispatcher remains the only entry point and error-conversion
boundary:

```text
request()
  -> validate selector, URL, protocol_info, and processors
  -> create common GatewayResponse and destination breaker
  -> protocol strategy.handle_request()
  -> preserve response material, raise typed expected failures
  -> request() finalizes one ok=True/False envelope
```

`JSONRPC` and `GRAPHQL` are semantic adapters over the existing HTTP
machinery, analogous to SOAP. They override one response-error decision hook
after the common HTTP transport has populated the envelope; they do not own a
second HTTP exchange loop. `S3` and `GRPC` are SDK-backed unary adapters.
Explicit FTPS is a configuration branch inside the existing FTP strategy,
not a new selector.

## Configuration summary

| Selector | Required configuration | Optional configuration |
|---|---|---|
| `FTP` | existing FTP keys | `tls_mode=implicit|explicit`; existing `verify_ssl` |
| `JSONRPC` | `method`, `request_id` | frozen R12 HTTP subset; `data` supplies params |
| `GRAPHQL` | `query` | `operation_name`, frozen R12 HTTP subset; `data` supplies variables |
| `S3` | `command`; operation-specific key/path | frozen R12 region/cap/list options |
| `GRPC` | `method`; explicit target URL port | frozen R12 metadata/serializer/timeout options |

## Error vocabulary

| Condition | Stable code | Status rule |
|---|---|---|
| JSON-RPC error object | `JSONRPC_ERROR` | preserve HTTP status, default 502 |
| Invalid JSON-RPC peer envelope | `JSONRPC_PROTOCOL` | 502 on successful HTTP response |
| GraphQL errors list | `GRAPHQL_ERROR` | preserve HTTP status, default 502 |
| Invalid GraphQL peer envelope | `GRAPHQL_PROTOCOL` | 502 on successful HTTP response |
| S3 service error | `S3_STATUS` | preserve AWS HTTP status, default 502 |
| Non-OK gRPC status | `GRPC_STATUS` | deterministic gRPC-to-HTTP map |
| Caller configuration/path error | existing `CONFIG`/`PATH` | existing rules |
| DNS/TLS/connect/timeout/size | existing transport code | existing rules |

## Implementation order

1. Add RED tests for one risk-ordered lane.
2. Add the smallest strategy/FTP change that satisfies that lane.
3. Run focused tests and the full regression suite at 100% coverage.
4. Obtain code-review approval before starting the next lane.
5. Serialize shared registry/error/fixture/documentation changes.
6. Run contract, security, clean-worktree build, and acceptance gates.

## Security and failure handling

- Validate every caller-controlled verb, mode, URL shape, cap, identifier,
  credential object, and local path before remote I/O where possible.
- Never provide an arbitrary attribute-to-SDK dispatch path.
- Never silently lower TLS verification, switch a named TLS mode to
  plaintext, or map `grpcs` to an insecure channel.
- Never echo gRPC call metadata or expose native gRPC debug strings.
- Preserve remote failure bodies/metadata before raising protocol errors.
- Let `CancelledError` propagate after closing response bodies/files/clients.
- Keep local/configuration failures out of breaker failure counts.

## Verification commands

```bash
pytest
flake8 asyncio_gateway tests examples
mypy asyncio_gateway
```

After commits, create a temporary clean worktree and run the repository's
exact `flake8 .` command there so ignored local environments and the user's
untracked agent tooling cannot contaminate the verdict. No live integration
credential is part of acceptance.
