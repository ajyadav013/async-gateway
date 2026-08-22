# Specification: Bounded Google Cloud Storage Selector

## Status and risk

Status: **Contract approved, including the generation-pinned ranged-download
design, private four-lease GCS offloader, 16-file plan, and
`google-cloud-storage>=3,<4` dependency; implementation remains pending the
independent architecture, EM, security, and test-planning gates.**

Risk: **High.** The feature handles Application Default Credentials (ADC),
Workload Identity, optional IAM service-account impersonation, and V4 signed
URLs that are bearer capabilities. It also adds a runtime dependency, a
private bounded thread executor, generation-sensitive ranged reads, and
bounded local file I/O. It is not restricted: the repository change is
reversible, performs no live GCP or destructive action, and excludes object
deletion and bucket administration.

Severity gate: implementation and delivery require zero unresolved Critical,
High, or Medium findings. Security review, test review, rollback instructions,
and a residual-risk statement are mandatory before release.

## Overview

Add a backend-only, first-class `GCS` strategy to the existing unary
`request(...) -> GatewayResponse` library contract. It supports five bounded
commands—`download`, `upload`, `head`, one-page `list`, and V4 `signed_url`—for
targets shaped as `gs://bucket/blob-or-prefix`. The selector reuses the
existing strategy registry, shared envelope, destination circuit breaker,
redaction, guarded local-file read, and atomic download writer. Downloads pin
one object generation and size before issuing sequential inclusive ranges, so
one local result cannot mix generations even when the latest object changes.
Download bytes are the object's exact stored/raw representation—including a
stored compressed representation—not an SDK- or GCS-transparently decompressed
body.

The feature authenticates only through Google ADC or Workload Identity.
Callers cannot submit service-account JSON, credential objects, custom API
endpoints, or project overrides. All Google SDK and credential work is
synchronous and therefore runs off the event loop with a finite
result-acceptance deadline, SDK timeouts wherever exposed, and
cancellation-safe draining/resource cleanup that may extend observed return
latency. A private four-worker/four-lease GCS offloader bounds synchronous
Google provider and credential work and fails excess admission promptly
without consuming the asyncio default executor. The unchanged bounded local
filesystem primitives retain their established internal default-executor use.
Tests use deterministic doubles and never contact GCP, a metadata server, or
an emulator.

## Assumptions

- This is a library strategy, not a FastAPI route and not a GCP deployment.
- The existing top-level `request()` signature and `GatewayResponse` key set
  remain unchanged.
- The gateway's existing destination key maps `gs://bucket/key` to a
  bucket-scoped breaker; a signed HTTPS URL never becomes a target or breaker
  key.
- `google-cloud-storage>=3,<4` is the only new direct runtime dependency. No
  mypy override is added unless a real typed-package failure proves one is
  necessary during implementation.
- V4 signed PUT enforcement is Cloud Storage behavior. Deterministic tests
  prove the exact signing arguments, while live enforcement is explicitly not
  an acceptance dependency.
- The resolved public spelling for expiry is `expires_in_seconds` everywhere.
- GCS execution capacity is a fixed private implementation safety limit, not
  public configuration. It introduces no new runtime dependency beyond the
  Python standard library and `google-cloud-storage>=3,<4`.

## Requirements

### R1: Public selector, dependency, target, and authentication contract

**Description**: Register `GCS` as a strict strategy backed by
`google-cloud-storage>=3,<4`, accepting only unambiguous `gs` targets and ADC
authentication.

**User Story**: As a gateway consumer, I want GCS operations to use the same
selector and envelope model as other protocols, so that cloud-object access
does not require a parallel client abstraction.

**Acceptance Criteria**:

- **AC1.1:** `resolve_protocol()` accepts case-insensitive `GCS`, maps it to
  `GcsRequest`, and the public scheme allowlist contains exactly `{'gs'}` for
  that selector; all existing selectors remain behaviorally unchanged.
- **AC1.2:** The target is `gs://bucket[/blob-or-prefix]`. Bucket is non-empty;
  query, fragment, user info, password, port, ambiguous authority, and every
  non-`gs` scheme are rejected after the preprocessor and before ADC, local
  file I/O, or breaker lookup/execution. Specifically, the existing dispatch
  scheme guard rejects a non-`gs` scheme first; after that guard passes,
  `GcsRequest.__init__` rejects the remaining target-shape violations before
  delegating to the base constructor's breaker lookup. A non-empty object key
  is required for every command except `list`, for which the empty prefix is
  valid.
- **AC1.3:** `auth` must be exactly `None`. Authentication uses ADC/Workload
  Identity only. Raw service-account JSON, mappings, credential objects,
  access tokens, key material, project overrides, custom endpoints, and custom
  hostnames are not accepted through request data or `protocol_info`.
- **AC1.4:** The runtime dependency is exactly
  `google-cloud-storage>=3,<4`; package metadata, wheel/sdist installation, and
  clean import checks include it without introducing an emulator dependency.
- **AC1.5:** The breaker destination remains the normalized GCS bucket, so
  one bucket's failures cannot open another bucket's circuit and no blob name,
  credential, service-account address, or signed URL enters the breaker key.

**Edge Cases**:

- `gs://bucket`, `gs://bucket/`, and an empty path are valid only for `list`.
- Percent-encoded text remains an object-name concern; parsing must not turn a
  path into a local filesystem path or silently decode it into a new target.
- Invalid selector, URL type, and strict `protocol_info` errors occur before
  the envelope and preprocessor. After the envelope and preprocessor, the GCS
  scheme guard runs; if it passes, `GcsRequest.__init__` validates the
  remaining target shape and auth. Both post-preprocessor validation seams run
  before an ADC call, metadata-server probe, client construction, local I/O,
  breaker lookup/execution, or network operation.

### R2: Strict command-specific option contract

**Description**: Validate and copy a frozen command-dependent
`protocol_info` contract at the public boundary before the preprocessor and
ADC.

**User Story**: As a gateway consumer, I want misspelled or misplaced GCS
options to fail immediately, so that invalid calls never touch local files or
cloud credentials.

**Acceptance Criteria**:

- **AC2.1:** `command` is required, is normalized from a non-empty string to
  lowercase, and must be one of `download`, `upload`, `head`, `list`, or
  `signed_url`; boolean and non-string values are rejected.
- **AC2.2:** The exact command allowlists are frozen by the following table.
  Every unknown key and every key belonging to another command is rejected
  before ADC, processors, local file I/O, breaker execution, or SDK work.

  | Command | Required keys | Optional keys |
  |---|---|---|
  | `download` | `command`, `local_path` | `max_response_bytes`, `if_generation_match`, `timeout`, `circuit_breaker_config`, `redact_query_params` |
  | `upload` | `command`, `local_path` | `max_upload_bytes`, `if_generation_match`, `timeout`, `circuit_breaker_config`, `redact_query_params` |
  | `head` | `command` | `if_generation_match`, `timeout`, `circuit_breaker_config`, `redact_query_params` |
  | `list` | `command` | `max_items`, `page_token`, `timeout`, `circuit_breaker_config`, `redact_query_params` |
  | `signed_url` GET | `command`, `method` | `expires_in_seconds`, `signing_service_account`, `timeout`, `redact_query_params` |
  | `signed_url` PUT | `command`, `method`, `content_type`, `max_upload_bytes` | `expires_in_seconds`, `signing_service_account`, `if_generation_match`, `timeout`, `redact_query_params` |

- **AC2.3:** `timeout` defaults to 15 seconds and must be a finite positive
  integer or float, with booleans rejected. `if_generation_match` must be a
  non-negative integer with booleans rejected; it is optional for `download`
  and `head`, and defaults to `0` for `upload` and signed PUT.
- **AC2.4:** `local_path` is a non-empty string. `max_response_bytes` and
  `max_upload_bytes` default to the repository's `MAX_RESPONSE_BYTES` where
  optional and must be positive integers with booleans rejected.
- **AC2.5:** `max_items` defaults to `1000` and must be an integer in
  `1..1000`, with booleans rejected. `page_token`, when present, must be a
  non-empty string whose UTF-8 encoding succeeds and is at most 4096 bytes.
  Its exact value—including leading, trailing, and whitespace-only content—is
  preserved without stripping, case conversion, Unicode normalization, or any
  other transformation. Invalid type, `''`, encoding failure, and a UTF-8
  representation above 4096 bytes are rejected by strict `protocol_info`
  validation before envelope, preprocessor, ADC, breaker, local I/O, or SDK.
  A valid token is opaque/secret and is never interpolated into diagnostics or
  logs.
- **AC2.6:** The validator returns a fresh validated mapping and never mutates
  the caller's mapping. It normalizes only fields whose contracts explicitly
  require normalization; a valid `page_token` is copied exactly unchanged.
  GCS remains strict even during the legacy 1.x warning window used by older
  selectors.

**Edge Cases**:

- Zero, negative, infinite, NaN, boolean, collection, and numeric-string
  limits/timeouts are rejected.
- `page_token=''` and tokens encoding to 4097 bytes are rejected; a token
  encoding to exactly 4096 bytes and any non-empty whitespace-only token are
  accepted and passed byte-for-byte unchanged. Multibyte test values prove the
  bound measures UTF-8 bytes rather than Python characters.
- A valid option used with the wrong command is an error, not an ignored hint.
- `circuit_breaker_config` is not accepted for `signed_url`.

### R3: Off-loop execution, result-acceptance deadlines, and cleanup

**Description**: Contain every remotely blocking synchronous Google provider
or credential seam so the async event loop is never blocked and cancellation
cannot orphan work, while reusing the bounded local filesystem primitives
unchanged.

**User Story**: As an async application operator, I want GCS calls to respect
deadlines and cancellation, so that credential refresh or storage I/O cannot
stall unrelated requests.

**Acceptance Criteria**:

- **AC3.1:** ADC discovery and refresh, transport-request creation, storage
  client construction, bucket lookup, blob lookup, storage operation, page
  fetch/iteration, metadata reload, ranged `download_as_bytes`, client close,
  impersonated-credential construction/refresh/signing, IAM `signBlob`, and
  URL generation execute only through the private GCS provider offloader
  specified in R13. Tests prove its thread identity at every provider seam and
  prove no Google provider/credential call consumes the asyncio default
  executor. `read_guarded_file` and `stream_to_path` remain the unchanged async
  bounded local-filesystem primitives and retain their established internal
  default-executor implementation; the GCS lifecycle lease remains held while
  either helper runs, but neither helper is submitted to the private GCS pool.
- **AC3.2:** Every remote SDK operation that exposes a timeout receives the
  validated finite SDK timeout. The outer deadline bounds whether a result is
  accepted: once it expires, no late worker result may become success.
  ADC/refresh/signing work that exposes no hard per-call timeout cannot be
  terminated with its Python worker thread; that worker is shielded and
  drained under the request's retained R13 lease and any late resource is
  closed off-loop before control returns.
  The selected google-auth/IAM credential implementation may perform internal
  signing retries within one gateway invocation of
  `Blob.generate_signed_url`; those SDK-internal requests are not gateway
  attempts and cannot be disabled without a custom signer that is outside
  scope.
  Consequently, cleanup after timeout or cancellation may extend observable
  wall-clock latency beyond `timeout`; the timeout is not a hard upper bound
  on return latency.
- **AC3.3:** Operational download/upload/head/list storage calls pass
  `retry=None`; the gateway circuit breaker is their sole retry owner and they
  disable provider retries at each of those four call sites. Signed URL
  generation bypasses that breaker, while its selected credential
  implementation may internally retry IAM signing as specified in AC3.2.
- **AC3.4:** Client and other owned resources close on success,
  refusal, malformed service output, timeout, service/transport error, and
  cancellation. Cleanup is drained under repeated cancellation and cannot
  replace the original body failure unless cleanup is the only failure. For
  `signed_url`, the storage client remains open through
  `Blob.generate_signed_url`, because the SDK may read its endpoint and
  universe-domain state. The returned URL stays in one private local variable
  while client close is shielded and drained. Only successful close permits
  atomic publication to `protocol_details`; close failure or cancellation
  before publication discards/sanitizes the local URL and exposes no
  URL-bearing state.
- **AC3.5:** Cancellation before an atomic download commit propagates as the
  original `CancelledError` after cleanup. Once the existing atomic writer has
  dispatched replacement, its established shield-and-drain linearization rule
  completes and reports the committed success honestly.
- **AC3.6:** No synchronous Google SDK/provider, ADC/credential, IAM signing,
  range-download, client construction/lookup/close, sleep, or provider-network
  operation is introduced directly on the event-loop path or submitted to
  asyncio's default executor. `gcs_client.py` introduces no direct local
  filesystem operation: it awaits the unchanged `read_guarded_file` and
  `stream_to_path` primitives while retaining the GCS lease. Their existing
  bounded filesystem behavior and internal default-executor use are explicitly
  permitted and are not redirected to the private GCS pool. The repository AST
  guard recognizes Google provider prefixes and rejects generic default-
  executor submission of provider work from `gcs_client.py` without imposing
  a new restriction on those two path helpers.

**Edge Cases**:

- Cancellation may arrive while a worker is starting, while it is returning a
  resource, during a metadata/range call, during close, or while a prior
  cancellation is being drained.
- A timeout does not imply that a Python worker thread stopped; the late
  result must be rejected, drained, and disposed safely, even when this makes
  observed return latency exceed the requested timeout.

### R4: Bounded atomic download

**Description**: Download one exact object's stored/raw bytes to a guarded
local path through the existing atomic streaming primitive, with transparent
decompression disabled.

**User Story**: As a gateway consumer, I want bounded GCS downloads that do
not leave corrupt files, so that remote failures and cancellation are safe.

**Acceptance Criteria**:

- **AC4.1:** `download` requires an exact non-empty object key and
  `local_path`. Its first transient-capable breaker attempt calls off-loop
  `blob.reload(retry=None, timeout=validated_timeout, ...)` to pin an exact
  non-negative integer generation, non-negative integer advertised size, and
  safe `etag`/`crc32c` metadata before any range request or local-file
  mutation. The pinned size is the object's stored/raw byte size. If the caller
  supplied `if_generation_match`, reload receives that exact value and the
  returned generation must equal it; the caller's generation is authoritative.
- **AC4.2:** A structurally retryable reload failure may use the gateway retry
  policy while no pin exists. Once generation, size, and metadata are pinned,
  they are immutable request state: every later gateway retry skips reload,
  restarts download offset at byte 0, and uses the same pin and advertised
  size. The request never repins to the latest object version.
- **AC4.3:** The private download chunk cap is exactly 64 KiB
  (`64 * 1024` bytes) and is not caller-configurable. Each off-loop inclusive
  range call is exactly
  `blob.download_as_bytes(start=offset, end=min(offset + 65536, pinned_size) - 1,
  if_generation_match=pinned_generation, raw_download=True, retry=None,
  timeout=validated_timeout)`. Calls advance sequentially only after the prior
  chunk has returned and validated. `raw_download=True` is mandatory on every
  bounded range call and is never omitted or set from caller input.
- **AC4.4:** Every range result must be `bytes` and have exactly the requested
  inclusive length; the accumulated final count must equal `pinned_size`.
  Short, overlong, non-bytes, negative/malformed metadata, or inconsistent
  final totals fail as `GCS_STATUS`/502 without publishing partial success. In
  particular, a transparent-decompression-shaped result larger than the exact
  requested raw range is an overlong malformed provider success and fails
  closed.
- **AC4.5:** Immediately after a successful pin, an advertised size above
  `max_response_bytes` raises the existing response-size error before the
  first range call, `stream_to_path`, path resolution, temporary-file creation,
  or local mutation. The cap is evaluated against the stored/raw pinned size;
  no transparent decompression can expand the transfer behind that bound. A
  zero-size object makes no range call and still produces an empty atomic
  download.
- **AC4.6:** Validated async chunks feed the unchanged
  `stream_to_path(local_path, chunks, overwrite=True,
  max_bytes=max_response_bytes, advertised_bytes=pinned_size)`. A range,
  validation, timeout, service, or pre-commit cancellation failure removes
  only the temporary file and leaves an existing target byte-for-byte
  unchanged. Only a structurally retryable range transport/service failure may
  gateway-retry the whole attempt, which restarts byte 0 against the same pin;
  malformed range data aborts without retry or breaker count. Only validated
  raw stored bytes are yielded to the writer; neither the SDK nor gateway
  transparently decompresses content. The request
  retains its GCS lifecycle lease while awaiting `stream_to_path`; the helper's
  existing internal default-executor file operations remain unchanged and do
  not run in the private GCS provider pool.
- **AC4.7:** Replacement of the latest object between chunks cannot mix
  generations because every range is conditioned on `pinned_generation`.
  A generation-related 404 or 412 aborts immediately without retry or breaker
  count. Ranged reads do not prove CRC integrity; `crc32c` is returned only as
  pinned service metadata and the gateway makes no checksum-verification
  claim. An object's `Content-Encoding` metadata does not alter the raw-byte
  range or output semantics.
- **AC4.8:** Success status is 200 and `protocol_details` is exactly
  `{command,bucket,key,local_path,bytes_written,etag,generation,crc32c}`.
  `bytes_written == pinned_size`, `generation` is the pinned non-negative
  integer, and `etag`/`crc32c` are normalized strings or `None`. The file's
  bytes are exactly the object's stored/raw representation, including its
  compressed representation when the object is stored with content encoding.

**Edge Cases**:

- A transient pin failure followed by success retries reload only until the
  pin exists. A transient second-range failure cleans the temporary file and
  retries from `start=0` without another reload.
- Replacement between two ranges either serves the same pinned generation or
  returns an abortable generation error; bytes from two generations can never
  share one destination.
- Empty, one-byte/single-range, exact-64-KiB, and multi-range objects exercise
  inclusive raw range boundaries. Short, overlong, non-bytes, malformed-size,
  malformed-generation, advertised-oversize, 404, and 412 responses are
  deterministic refusal cases.
- Gzip or other `Content-Encoding` metadata never triggers transparent
  decompression. Deterministic doubles return stored compressed bytes and fail
  the call if `raw_download=True` is absent; a double that instead returns an
  oversized decompressed-shaped range is rejected as overlong.

### R5: Guarded upload with generation safety

**Description**: Upload one bounded local regular file without allowing SDK
re-open or accidental overwrite.

**User Story**: As a gateway consumer, I want uploads to be bounded and
create-only by default, so that retries cannot silently replace cloud data.

**Acceptance Criteria**:

- **AC5.1:** `upload` requires an exact non-empty object key and
  `local_path`. It invokes `read_guarded_file` exactly once before breaker
  execution, using `max_upload_bytes`; the SDK receives the returned bounded
  bytes and never reopens the caller's pathname. The GCS lifecycle lease is
  held while awaiting the unchanged helper, whose existing internal default-
  executor file operations do not run in the private GCS provider pool.
- **AC5.2:** `if_generation_match` defaults to `0` (create-only) and the same
  validated value is supplied on every gateway retry. Callers may explicitly
  provide another non-negative generation to perform an optimistic-concurrency
  write.
- **AC5.3:** Upload calls pass `retry=None` and the validated timeout. Local
  path, symlink, identity, regular-file, and size failures occur before the
  breaker attempt and are never counted or retried.
- **AC5.4:** Success status is 200 and `protocol_details` is exactly
  `{command,bucket,key,local_path,bytes_read,etag,generation,metageneration,crc32c}`.
  `bytes_read` is the guarded observed count and is non-negative.

**Edge Cases**:

- Empty files are valid even though the configured cap must be positive.
- A retry after an ambiguous transport failure remains safe only because the
  same generation precondition is replayed.
- Symlinks, non-regular files, identity swaps, and files that grow beyond the
  cap are refused by the existing guarded read.

### R6: Normalized object head

**Description**: Retrieve one object's metadata without exposing SDK objects.

**User Story**: As a gateway consumer, I want stable object metadata, so that
my code does not depend on Google SDK response types.

**Acceptance Criteria**:

- **AC6.1:** `head` requires an exact non-empty object key and issues one
  metadata fetch with optional `if_generation_match`, `retry=None`, and the
  validated timeout.
- **AC6.2:** Success status is 200 and `protocol_details` is exactly
  `{command,bucket,key,content_length,content_type,etag,generation,metageneration,last_modified,crc32c,metadata}`.
- **AC6.3:** `content_length` is a non-negative integer or `None`;
  generation values are non-negative integers or `None`; timestamps are
  ISO-8601 strings or `None`; scalar strings are strings or `None`; and
  `metadata` is a new mapping of string keys to string values. Any malformed
  SDK success value is `GCS_STATUS`/502 rather than a leaked SDK object.

**Edge Cases**:

- Missing optional metadata normalizes to `None` or `{}` without changing the
  schema.
- Empty strings returned in fields that require meaningful service values are
  malformed success data and fail closed.

### R7: One-page bounded list

**Description**: List at most one service page under one optional prefix.

**User Story**: As a gateway consumer, I want explicit bounded pagination, so
that one call cannot enumerate an unbounded bucket.

**Acceptance Criteria**:

- **AC7.1:** `list` treats the target path as a prefix, including the empty
  prefix, and requests exactly one page using `max_items` and optional opaque
  `page_token`; the validated token is passed to the SDK exactly as supplied,
  including whitespace, and it never recursively follows a returned token.
- **AC7.2:** Page construction, page fetch/iteration, and close all occur
  off-loop with `retry=None` and the validated timeout. Service order is
  preserved.
- **AC7.3:** Success status is 200 and `protocol_details` is exactly
  `{command,bucket,prefix,items,item_count,is_truncated,next_page_token}`.
  `item_count == len(items)`, `is_truncated` is boolean, and
  `next_page_token` is a non-empty string or `None`.
- **AC7.4:** Every item is exactly
  `{key,size,content_type,etag,generation,last_modified,crc32c}`. `key` is a
  non-empty string, `size` is a non-negative integer, and optional fields use
  the normalized types in the data-model section. One malformed page or item
  makes the whole call `GCS_STATUS`/502.

**Edge Cases**:

- An empty page is successful with `items=[]`, `item_count=0`, and a coherent
  truncation/token pair.
- A caller page token and a server next-page token are bearer-like opaque
  values. The caller token is accepted only under AC2.5's 4096-byte UTF-8 cap
  and is never echoed. Only the server token may appear verbatim, and only in
  successful `protocol_details.next_page_token`.

### R8: Error vocabulary, precedence, retry, and breaker semantics

**Description**: Normalize GCS failures into the existing envelope and retry
model without leaking provider internals.

**User Story**: As a gateway consumer, I want stable GCS failure codes and
statuses, so that retry and remediation decisions do not parse SDK messages.

**Acceptance Criteria**:

- **AC8.1:** Add `GcsStatusError(ProtocolError)` with wire-stable code
  `GCS_STATUS`; its default status is 502 and it is a warning-class remote
  response in the central status map.
- **AC8.2:** A Google service exception preserves a structurally valid HTTP
  status in `100..599`; otherwise it reports 502. Exact safe service-failure
  details are
  `{command,bucket,target,gcs_error_code,gcs_error_message,response_metadata}`,
  where `target` is the object key or list prefix and `response_metadata` is
  exactly `{http_status_code,request_id}`. Values are normalized/redacted
  strings or `None`; SDK response objects never enter the envelope.
- **AC8.3:** Only status 408, 429, 500, 502, 503, or 504 is a retryable,
  breaker-counted GCS service response. All other service statuses—including
  400, 401, 403, 404, 409, and 412—abort without retry or breaker count.
  In particular, a pinned-download generation 404 or 412 is always abortable.
  Classification is structural and never parses message text.
- **AC8.4:** DNS, TLS, connect, timeout, and other transport failures map to
  the existing transport vocabulary and existing breaker policy. Missing ADC,
  incomplete credentials, non-signing direct credentials, local path/cap
  failures, and caller configuration failures use existing typed configuration
  or local errors and are not counted. GCS offloader saturation/closing uses
  `GcsCapacityError`/`GCS_CAPACITY`/503 and is likewise never counted or
  retried.
- **AC8.5:** Failure precedence is: selector, URL-type, and strict
  `protocol_info` validation before envelope/preprocessor; GCS scheme guard
  after the preprocessor; remaining target/auth validation in
  `GcsRequest.__init__`; base-constructor breaker lookup; nonblocking GCS
  capacity admission; guarded local upload failure before ADC/breaker
  execution; exact body/service/transport failure over later cleanup failure;
  original cancellation over cleanup; real valid service status over default
  502; malformed nominal-success metadata as `GCS_STATUS`/502. Capacity
  rejection is used only for saturated or closing admission, not as a
  substitute for another known failure. Programming defects still propagate
  through the existing one-conversion-point invariant.
- **AC8.6:** With no retry policy, an operational command makes one storage
  attempt. With gateway `allowed_retries=N`, only retryable failures permit at
  most `N+1` attempts. Upload generation preconditions remain identical across
  attempts. Download retries may repeat transient reload only until a pin
  exists; after pinning, they reuse immutable generation/size metadata, skip
  reload, and restart byte 0. `signed_url` bypasses the breaker and gateway
  retry machinery and invokes `Blob.generate_signed_url` exactly once per
  gateway call. The
  selected credential implementation may internally retry IAM signing inside
  that one invocation. Once a URL has been returned into the strategy's
  private local state, neither cleanup failure nor cancellation starts another
  gateway invocation or signing attempt.

**Edge Cases**:

- Cleanup is not permitted to suppress or replace a body failure.
- HTTP status values outside `100..599`, booleans, strings, and malformed
  provider metadata are not trusted.
- Authentication/authorization failures are remote application responses, not
  retryable transport failures.

### R9: V4 signed URL common and GET contract

**Description**: Generate one exact-object V4 GET or PUT URL while treating
the result as a short-lived bearer secret.

**User Story**: As a gateway consumer, I want constrained signed URLs, so that
clients can transfer an object without receiving gateway credentials.

**Acceptance Criteria**:

- **AC9.1:** `signed_url` requires an exact non-empty object target and
  `method` equal to case-insensitive `GET` or `PUT`, normalized to uppercase.
  POST, DELETE, HEAD, custom methods, prefixes, wildcard targets, signed POST
  policies, custom endpoints/hostnames, arbitrary headers, and arbitrary
  query parameters are rejected.
- **AC9.2:** `expires_in_seconds` defaults to `900` and must be an integer in
  `1..3600`, with booleans rejected. The exact normalized value is used for
  V4 expiration and returned in details. For both GET and PUT, the gateway
  binds the relative duration to the SDK exactly as
  `expiration=timedelta(seconds=expires_in_seconds)`; it never passes the raw
  integer as an absolute timestamp. Exact-call tests cover 1, 900, and 3600
  seconds.
- **AC9.3:** Without `signing_service_account`, ADC credentials must implement
  Google's signing capability and supply a usable signer identity. Bearer-only
  user credentials and non-signing Workload Identity credentials fail safely
  before URL generation.
- **AC9.4:** Optional `signing_service_account` is 1..253 characters and must
  fully match
  `[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com`.
  It creates scoped impersonated credentials from ADC and signs through IAM
  `signBlob`; no caller-supplied delegates, scopes, lifetime, token, or
  credential payload is accepted.
- **AC9.5:** The gateway invokes `Blob.generate_signed_url` exactly once with
  the exact object, method `GET`,
  `expiration=timedelta(seconds=expires_in_seconds)`, selected signing
  credentials, and no arbitrary signed headers/query parameters. The selected
  google-auth/IAM credential path may internally retry signing within that
  invocation. The storage client remains open until generation returns and
  its shielded, drained close succeeds; only then does success status 200
  expose details exactly
  `{command,method,bucket,key,expires_in_seconds,signed_url}`.

**Edge Cases**:

- Expiry values 1 and 3600 are valid; 0, 3601, floats, booleans, and numeric
  strings are invalid.
- A syntactically valid but unauthorized impersonation target becomes a safe
  signing/service failure with no URL-bearing partial detail.

### R10: Constrained V4 signed PUT contract

**Description**: Sign content type, upload-size range, and generation safety
into a direct-upload URL.

**User Story**: As a gateway consumer, I want a bounded create-only signed PUT
grant, so that a client cannot use it for a different content type, size, or
unconstrained overwrite.

**Acceptance Criteria**:

- **AC10.1:** `content_type` is required and is 1..255 visible-ASCII
  characters (`0x21..0x7E`), contains exactly one non-empty type/subtype
  separator `/` before any optional parameter text, and contains no space,
  CR, LF, control, or non-ASCII character. `max_upload_bytes` is a required
  positive integer with booleans rejected.
- **AC10.2:** `if_generation_match` defaults to `0` and must be a non-negative
  integer. The gateway invokes `Blob.generate_signed_url` exactly once with
  method `PUT`, `expiration=timedelta(seconds=expires_in_seconds)`, and the
  dedicated SDK argument `content_type=content_type`. Its SDK `headers`
  argument contains exactly two entries:
  `{'x-goog-content-length-range': '1,<max_upload_bytes>',
  'x-goog-if-generation-match': str(if_generation_match)}`. It does not also
  put `content-type` in the SDK header mapping, because the SDK derives that
  signed header from the dedicated argument. No arbitrary or
  generation-related query parameter is signed or returned.
- **AC10.3:** Success status is 200 and details are exactly
  `{command,method,bucket,key,expires_in_seconds,signed_url,content_type,max_upload_bytes,if_generation_match,required_headers}`.
  Independently of the two-entry SDK `headers` argument, the public
  `required_headers` client contract is exactly
  `{'content-type': content_type, 'x-goog-content-length-range':
  '1,<max_upload_bytes>', 'x-goog-if-generation-match':
  str(if_generation_match)}` and tells the client all three headers it must
  send.
- **AC10.4:** Signed URL creation makes exactly one gateway invocation of
  `Blob.generate_signed_url`, with no breaker or gateway retry; the selected
  credential implementation may internally retry IAM signing inside it. The
  storage client remains open through generation and is then closed off-loop
  under shield/drain. The URL remains a private local bearer until close
  succeeds, then is atomically published to successful `protocol_details`.
  Cleanup failure or cancellation before publication discards/sanitizes the
  URL, exposes no URL-bearing response/error/log/trace state, and never retries
  generation. No gateway retry occurs after a URL is produced.
- **AC10.5:** Deterministic tests assert the exact V4 generation call and
  result schema, exactly one gateway SDK invocation, supported
  credential-internal retry semantics, client-open-through-generation, and
  close-before-publication behavior. They separately assert the dedicated SDK
  `content_type`, exact two-entry SDK `headers`, exact three-entry public
  `required_headers`, and `timedelta` expiry conversion at 1, 900, and 3600.
  Documentation explicitly states that Cloud Storage—not this no-live-GCP
  suite—enforces the three client-supplied signed headers, length range,
  generation precondition, permissions, and expiry.

**Edge Cases**:

- `max_upload_bytes=1` signs `x-goog-content-length-range: 1,1`.
- The SDK `headers` mapping has exactly two entries and no Content-Type key;
  `content_type=content_type` is the sole SDK input for that signed header,
  while public `required_headers` still has all three lowercase client values.
- Header names are lowercase in returned details; callers must send all three
  exact headers and cannot add, remove, or override signed headers or supply
  arbitrary signed query parameters.
- Signed PUT does not upload bytes through the gateway and does not validate
  client content.

### R11: Envelope, normalization, and bearer-secret containment

**Description**: Preserve the common envelope while returning only exact,
JSON-safe GCS details and containing bearer material.

**User Story**: As a security-conscious consumer, I want GCS outputs to be
stable and secrets narrowly exposed, so that routine diagnostics do not leak
credentials or signed grants.

**Acceptance Criteria**:

- **AC11.1:** Every command fills and returns the existing shared
  `GatewayResponse`; no top-level key is added or removed. GCS-specific data
  appears only under `protocol_details`; top-level `url` remains the redacted
  caller `gs://` target.
- **AC11.2:** Every successful command uses exactly the detail schema stated
  in R4–R7 and R9–R10. Every field is normalized to finite JSON-safe scalar,
  list, or string mapping data; no SDK client, credential, datetime, iterator,
  page, exception, or response object is retained.
- **AC11.3:** A signed URL is a bearer secret. Its complete value exists only
  at successful `protocol_details['signed_url']`; it is absent from top-level
  `url`, payload echo, messages, errors, causes, exception args/contexts,
  tracebacks, logs, tracer callbacks, breaker keys/state, and every failure or
  cancellation detail.
- **AC11.4:** ADC tokens, credential-provider details, service-account key
  material, IAM signatures, caller `page_token`, and signed query values never
  appear in public failure surfaces or logs. The server's `next_page_token`
  survives verbatim only in a successful list detail because it is needed for
  the caller's next explicit page.
- **AC11.5:** Signed URL details are assembled atomically only after all
  validation, ADC, refresh, impersonation, signing, URL generation, and
  shielded/drained client cleanup succeed. The generated URL remains private
  local state until that point and is then atomically published. Any failure
  or cancellation before publication discards/sanitizes it and leaves
  `signed_url` absent from every response/error/log/trace surface; generic
  redaction never replaces the successful published bearer with a sentinel.

**Edge Cases**:

- Provider error text containing a token, service-account address, V4 query,
  or embedded URL is redacted before it can become an error message or cause.
- A post-processor is caller code and remains governed by the existing
  processor contract; the GCS strategy itself never copies the bearer URL to
  another surface.

### R12: Tests, documentation, packaging, and quality gates

**Description**: Prove the public contract without live cloud dependencies
and keep registry, docs, package metadata, and examples synchronized.

**User Story**: As a maintainer, I want deterministic comprehensive coverage,
so that GCS behavior can ship without credentials and without regressing the
existing protocol matrix.

**Acceptance Criteria**:

- **AC12.1:** Development follows RED/GREEN TDD. Focused tests first fail for
  the absent GCS behavior, then pass with the smallest implementation; tests
  cover every numbered acceptance criterion, command, boundary, malformed
  shape, service/transport class, retry/breaker path, cancellation point,
  cleanup outcome, normalization rule, and secret surface. Signed-URL tests
  prove one gateway `Blob.generate_signed_url` invocation, no breaker/gateway
  retry, allowance for credential-internal IAM retries, client open during
  generation, shielded/drained close before publication, and complete URL
  absence on cleanup failure or pre-publication cancellation. Exact signing
  tests distinguish PUT's dedicated SDK `content_type` plus two-entry SDK
  `headers` from the three-entry public client `required_headers`, and assert
  `expiration=timedelta(seconds=expires_in_seconds)` for GET/PUT at 1, 900,
  and 3600. List-validation
  tests reject non-string, `''`, UTF-8 encoding failure, and 4097-byte tokens;
  accept exact 4096-byte and non-empty whitespace-only tokens; and prove the
  exact supplied value reaches the SDK without normalization. Download tests
  prove transient reload retry only until a pin exists, caller-generation
  authority, immutable generation/size across retries, range retry from byte
  zero, replacement between chunks without mixed-generation output, the exact
  64-KiB inclusive ranges, empty and advertised-oversize behavior, and aborts
  for short/overlong/non-bytes chunks, malformed pin metadata, and generation
  404/412. Exact-call doubles require `raw_download=True` together with exact
  `start`, inclusive `end`, pinned `if_generation_match`, `retry=None`, and
  finite timeout for one-byte/single-range, exact-64-KiB, multi-range,
  post-pin retry, and generation-pinned replacement cases. Gzip/content-
  encoding range/cap tests return stored compressed bytes, fail if the raw flag
  is absent, and reject an overlong transparent-decompression-shaped result;
  they make no live-GCP enforcement claim. Capacity tests prove every behavior
  in AC13.8, including that four
  blocked private provider workers do not starve unrelated/default-executor
  work and without asserting that the unchanged path helpers avoid their
  established internal default executor.
- **AC12.2:** `tests/logic/test_gcs_client.py` uses deterministic ADC,
  credential, storage-client, bucket, blob metadata/range, page,
  impersonation, IAM signer, clock, executor-lease, future, shutdown, and
  telemetry doubles. Range doubles capture and strictly compare the complete
  call, including `raw_download is True`, and model content-encoded stored bytes
  without contacting GCP. Existing path-helper seams prove bounded local work
  is awaited under the lease but not submitted to the private provider pool.
  Tests never use live GCP, credentials, metadata server, DNS/network,
  emulator, or real bucket. An external live integration test may exist only
  behind an explicit opt-in marker/environment gate and is never required by
  CI.
- **AC12.3:** Registry/scheme/strict-allowlist matrices, global hostile
  invariants, exact envelope rows, exception/status maps, provider-blocking-I/O
  and default-executor-isolation checks, documentation inventories, example
  inventories, runtime metadata, wheel/sdist installation, clean imports, and
  `GCS_CAPACITY` status/logging behavior all include `GCS` without changing or
  newly prohibiting the path helpers' internal executor use.
- **AC12.4:** The full suite passes with the repository's 100% statement and
  branch coverage policy. Python 3.10–3.14 CI, shuffled-order tests, coverage
  ratchets, committed-tree and clean-worktree flake8, mypy, formatting,
  suppression policy, dependency checks, build, and artifact install/import
  checks remain green.
- **AC12.5:** README and `examples/gcs_example.py` document all five commands,
  exact allowlists and result schemas, ADC/Workload Identity setup, least
  privilege, impersonation permissions, timeout/retry ownership, generation
  safety, signed URL bearer handling, expiry/revocation limitation, required
  PUT headers, the two-entry SDK-versus-three-entry client header distinction,
  relative `timedelta` expiry binding, the exact opaque `page_token` UTF-8
  4096-byte cap/no-normalization rule, immutable generation-pinned 64-KiB
  ranged downloads and their no-CRC-verification claim, fixed private
  four-lease provider capacity and shutdown semantics, unchanged local path-
  helper/default-executor behavior under that lease, exact stored/raw download
  semantics with transparent decompression disabled, no-live-test policy, and
  the out-of-scope boundaries.
- **AC12.6:** CHANGELOG records the additive selector and dependency. No CI
  workflow, database, deployment, GCP resource, Kubernetes object, or release
  tag is changed by this feature.

**Edge Cases**:

- Tests exhaustively scan the returned envelope, captured logs, exceptions,
  causes, tracebacks, tracer state, and breaker state for a sentinel signed URL
  and known credential/token values.
- Source-only artifact builds may become slower because transitive packages
  such as `google-crc32c` compile; this is measured but is not solved by
  weakening artifact checks.

### R13: Isolated bounded GCS execution capacity

**Description**: Isolate remotely blocking synchronous Google provider,
credential, and signing work behind one fixed private executor and four
nonblocking request leases so stalled Google work cannot consume asyncio's
default executor or accumulate an admission queue. The request lease also
spans the unchanged bounded local path helpers, but those helpers keep their
existing internal executor behavior.

**User Story**: As an async application operator, I want GCS saturation to
fail locally and promptly, so that unrelated protocols retain scheduling
capacity and accepted GCS requests can always drain and close their resources.

**Acceptance Criteria**:

- **AC13.1:** `gcs_client.py` privately owns exactly one
  `ThreadPoolExecutor(max_workers=4,
  thread_name_prefix='asyncio-gateway-gcs')`. The private provider adapter
  never uses or modifies the event loop's default executor for Google
  SDK/provider, ADC/credential, IAM signing, range-download, client
  construction/lookup/close, or other remotely blocking provider work, and
  there is no new shared offloader module, process boundary, public capacity
  option, environment setting, or runtime tuning surface. This isolation rule
  does not apply to the unchanged `read_guarded_file` and `stream_to_path`
  internals.
- **AC13.2:** The module privately owns exactly four logical permits, enforced
  with a nonblocking `threading.BoundedSemaphore(4)` admission. After the base
  constructor has looked up the breaker, each request must acquire one lease
  before guarded local I/O, ADC, breaker execution, SDK submission, or other
  GCS synchronous work. There is no waiting admission queue: when all four
  leases are held or shutdown has closed admission, the request is rejected
  promptly rather than awaiting capacity.
- **AC13.3:** One admitted request retains its lease across its entire GCS
  synchronous lifecycle, including deadline/cancellation draining and every
  owned provider-resource close, and while it awaits either bounded local path
  helper. A lease permits at most one outstanding private GCS-provider executor
  future at a time; provider offloads within a request are sequential. The
  lease is released only after every private provider future has finished,
  every provider resource it owns has closed, and any invoked path helper has
  returned or completed its established drain/cleanup. Cleanup never needs to
  reacquire GCS capacity and at most four private GCS-provider futures can be
  outstanding. Path-helper internal default-executor futures are not submitted
  to or counted as private GCS-provider futures.
- **AC13.4:** Saturated or closing admission raises the new typed
  `GcsCapacityError(AsyncGatewayError)` with wire-stable code
  `GCS_CAPACITY`, status 503, and no target-derived or secret-bearing details.
  It occurs before ADC/local I/O/breaker execution and is never gateway-
  retried or breaker-counted. `GCS_CAPACITY` is a local error-level condition,
  not a warning-class remote response.
- **AC13.5:** Expiry of the result-acceptance deadline or cancellation of the
  async waiter never releases a lease while its private provider future or an
  invoked path helper's established internal work is still running. Provider
  futures are shielded/drained and any late provider resource is closed through
  the same retained lease; path helpers preserve their existing drain/cleanup
  semantics. Only completion of all such work permits lease release. Repeated
  cancellation preserves the original outcome and cannot over-release the
  bounded semaphore.
- **AC13.6:** A private idempotent shutdown path first atomically marks
  admission closing. It calls
  `executor.shutdown(wait=False, cancel_futures=True)` exactly once when the
  active-lease count reaches zero—immediately when already idle, or from the
  final lease release after active requests finish—so closing cannot revoke
  cleanup capacity from an accepted request. Calls already running cannot be
  killed, not-yet-started executor futures are cancelled when shutdown runs,
  and Python process exit may still wait for live executor threads. Every SDK
  operation that exposes a timeout continues to receive the finite validated
  timeout.
- **AC13.7:** Existing logging/metrics seams expose only the events
  `gcs_capacity_state` (`active_leases`, `max_leases=4`),
  `gcs_capacity_rejected` (`active_leases`, reason `saturated` or `closing`),
  `gcs_drain_started`, `gcs_drain_finished` (`active_leases`, finite
  `duration_seconds`), and `gcs_capacity_closing` (`active_leases`). Capacity
  state is emitted on successful acquire/release, rejection on every failed
  admission, and closing on the single close transition. Drain events are
  emitted only when work continues beyond the outer acceptance deadline or
  cancellation. These events never include bucket, object/prefix, caller or
  server page token, signer identity, credential/token data, payload, signed
  headers/query values, or signed URL.
- **AC13.8:** Deterministic tests occupy four leases with blocked workers and
  prove a fifth request and a post-shutdown request fail promptly with
  `GCS_CAPACITY`/503 and no ADC/breaker call. They also prove one-future-per-
  lease provider sequencing, cancellation retains a lease until its provider
  worker/close or invoked path helper finishes, cleanup uses retained capacity,
  shutdown is idempotent, and telemetry is exact and secret-safe. With all four
  private GCS provider workers blocked, unrelated work—and the existing path-
  helper filesystem work—submitted through
  `asyncio.to_thread` or `run_in_executor(None, ...)` still schedules while
  all four GCS workers are occupied. Tests must not assert that
  `read_guarded_file` or `stream_to_path` avoids its existing default-executor
  implementation.

**Edge Cases**:

- Four requests may hold leases while between sequential SDK calls; a fifth
  still fails promptly because admission bounds complete request lifecycles,
  not merely currently executing calls.
- Shutdown racing with admission has one linearized result: the request owns a
  lease before closing begins, or it receives `GCS_CAPACITY`; no lease is lost
  or double-released.
- Four unkillable synchronous workers can keep all GCS capacity occupied and
  can delay process exit, but they neither enqueue more admitted GCS requests
  nor prevent unrelated default-executor work from scheduling.

## Dependencies

- New direct runtime dependency: `google-cloud-storage>=3,<4`.
- Python standard-library `concurrent.futures.ThreadPoolExecutor`,
  `threading.BoundedSemaphore`, and `datetime.timedelta`; no additional
  offloading, queueing, or checksum package is introduced.
- Existing reusable modules:
  `BaseRequestClass.validate_protocol_info`, `destination_of` and the breaker
  registry, `GatewayResponse`/`finalise_ok`/`finalise_error`, central redaction
  helpers, `read_guarded_file`, and `stream_to_path`.
- Runtime environment supplies ADC through local developer credentials,
  attached service-account identity, or Workload Identity. Signed URLs require
  either signing-capable direct credentials or permission to impersonate the
  validated service account and invoke IAM `signBlob`.
- No schema, database, cache, frontend, emulator, GCP resource, or deployment
  dependency.

## Out of Scope

- Delete, copy, move, rewrite, compose, multipart/resumable API exposure,
  bulk operations, ACL, IAM policy administration, retention/lifecycle,
  bucket creation/configuration, and public-object URLs.
- Signed POST, policy documents, DELETE/HEAD/custom signed methods, arbitrary
  headers/query parameters, CDN/custom endpoints, and caller-selected hosts.
- Recursive pagination, automatic page following, streaming public APIs,
  directory synchronization, globbing, and prefix-as-object ambiguity.
- Raw credentials, service-account JSON/key files, access-token injection,
  custom scopes/delegates/lifetimes, and arbitrary project overrides.
- Pub/Sub, Cloud Tasks, Secret Manager, BigQuery, Cloud Run, GKE, GCP
  deployment, SIT tagging, Kubernetes changes, and live GCP acceptance tests.
- Public/configurable executor size, queued admission, a shared offloader
  helper, process pool or subprocess boundary, caller-selected chunk size,
  recursive/range streaming API, and a new checksum-verification claim.

## Open Questions

None. The approved public contract is frozen. Any implementation evidence
that requires a public-contract change, an additional dependency, a CI edit,
or a file outside the inventory below must stop and return to specification
review rather than improvising.

---

# Developer Documentation: Bounded Google Cloud Storage Selector

## Architecture Overview

GCS follows the repository's existing Strategy pattern and one-conversion
boundary:

```text
request()
  -> resolve selector + validate URL type and strict protocol_info
  -> create the shared redacted envelope
  -> run preprocessor
  -> apply the GCS -> gs dispatch scheme guard
  -> GcsRequest validates remaining target shape and auth
  -> BaseRequestClass performs the bucket-scoped breaker lookup
  -> nonblocking acquire of one private GCS lifecycle lease
     -> saturation/closing: GCS_CAPACITY/503; no I/O or breaker execution
  -> lease remains held across all provider and bounded local-helper work
     -> upload: await unchanged read_guarded_file before ADC/breaker execution
     -> private four-worker offloader for ADC/credentials/IAM/client/SDK,
        provider lookup/operation/ranges, and provider-resource close
        -> download: pin generation/stored size once; conditioned <=64-KiB
           ranges with raw_download=True feed unchanged stream_to_path under
           the same lifecycle lease
        -> other operational command: breaker.run(one retry=None SDK attempt)
        -> signed_url: keep client open; invoke Blob.generate_signed_url once
           (credential implementation may internally retry IAM signing)
           -> shield/drain client close
           -> atomically publish private local URL only after close succeeds
     -> both path helpers retain established bounded default-executor
        filesystem behavior; neither enters the private GCS provider pool
  -> release lease only after provider futures/resources and path helpers finish
  -> normalize exact JSON-safe protocol_details
  -> finalise_ok or raise one typed error for request() to finalise
```

The strategy is one module, parallel to `s3_client.py`. It does not create a
second envelope, file-transfer primitive, breaker registry, redaction system,
or credential abstraction. Synchronous SDK objects remain private and never
cross the strategy boundary. Existing local path primitives remain unchanged;
the dedicated pool isolates provider work rather than replacing their internal
offloading.

## File Structure and Blast Radius

The approved plan is capped at these **16 files**:

```text
asyncio_gateway/
  asyncio_gateway.py                         MODIFIED — GCS -> gs scheme guard
  logic/
    __init__.py                              MODIFIED — register GCS strategy
    gcs_client.py                            NEW — strict GCS strategy/private bounded offloader
  utils/
    exceptions.py                           MODIFIED — GCS status/capacity errors
    status_map.py                            MODIFIED — GCS_STATUS/GCS_CAPACITY mappings
examples/
  gcs_example.py                            NEW — bounded ADC-backed examples
tests/
  fixtures/protocol_transports.py           MODIFIED — deterministic GCS contract seam
  logic/test_gcs_client.py                   NEW — focused contract and failure suite
  test_docs.py                               MODIFIED — selector/options/example anti-drift
  test_entrypoint.py                         MODIFIED — registry/scheme/validation matrices
  test_exceptions.py                         MODIFIED — hierarchy/status assertions
  test_no_blocking_io.py                     MODIFIED — Google provider isolation guard
  test_packaging.py                          MODIFIED — dependency/artifact/example checks
CHANGELOG.md                                 MODIFIED — additive feature/dependency note
README.md                                    MODIFIED — complete GCS consumer/security docs
pyproject.toml                               MODIFIED — google-cloud-storage>=3,<4
```

`tests/test_entrypoint_invariant.py` consumes the extended fixture without a
required edit. `asyncio_gateway/utils/paths.py` is reused unchanged. If either
needs modification, implementation must stop and obtain approval for a revised
17- or 18-file plan.

Blast radius: one new public selector and stable error code; one dependency;
registry/scheme/doc/example/package inventories; shared breaker and envelope
execution but no schema, database, route, CI, deployment, or infrastructure
change.

## Data Models

### Input contract

| Field | Type | Required | Validation/default |
|---|---|---:|---|
| `url` | string | yes | `gs://bucket[/object-or-prefix]`; object required except list |
| `auth` | exactly `None` | yes | ADC/Workload Identity only |
| `command` | string enum | yes | normalized lowercase; five values |
| `local_path` | string | download/upload | non-empty; existing guarded path policy |
| `timeout` | int/float | no | finite positive, bool rejected; default 15 |
| `max_response_bytes` | integer | download | positive; default `MAX_RESPONSE_BYTES` |
| `max_upload_bytes` | integer | upload optional, signed PUT required | positive; bool rejected |
| `if_generation_match` | integer | no | non-negative; upload/signed PUT default 0 |
| `max_items` | integer | list | 1..1000; default 1000 |
| `page_token` | string | no | non-empty; UTF-8 encoding <=4096 bytes; preserve exact value including whitespace; opaque and secret |
| `method` | string enum | signed URL | GET or PUT, normalized uppercase |
| `expires_in_seconds` | integer | no | 1..3600; default 900; SDK receives `timedelta(seconds=value)` |
| `signing_service_account` | string | no | anchored service-account address, max 253 |
| `content_type` | string | signed PUT | visible ASCII media type, max 255 |
| `circuit_breaker_config` | mapping | operational commands only | existing closed validator |
| `redact_query_params` | collection | no | existing normalization policy |

### Normalized output scalar rules

| Field family | Normalized type |
|---|---|
| `bytes_written`, `bytes_read`, `content_length`, `size` | non-negative integer (`content_length` may be `None`) |
| `generation`, `metageneration` | non-negative integer or `None` |
| `etag`, `crc32c`, `content_type` | string or `None` |
| `last_modified` | ISO-8601 string with offset or `None` |
| custom object `metadata` | `dict[str, str]` |
| page `items` | list of exact item mappings in service order |
| `next_page_token` | non-empty string or `None` |
| `required_headers` | exact three-entry lowercase `dict[str, str]` containing content type, length range, and generation match |
| `signed_url` | full string only in successful signed-url details |

For a successful download, `generation` is required (not `None`) and equals
the immutable non-negative pinned generation; `bytes_written` equals the
immutable pinned stored/raw size. The destination contains the exact stored/raw
representation, including stored compressed bytes; transparent decompression
is never part of the download contract. Its `crc32c` is optional pinned
metadata only. Ranged reads in this design do not independently verify that
checksum, so neither the model nor documentation labels the value as gateway-
verified integrity.

### Private lifecycle state

| State | Type/invariant | Lifetime |
|---|---|---|
| download pin | non-negative `generation` and `size`, plus normalized `etag`/`crc32c`; set once | first successful reload through request completion |
| range offset | integer `0..pinned_size`; resets to 0 on a post-pin gateway retry | one download attempt |
| GCS lease | one of exactly four nonblocking permits; at most one outstanding private provider future | admission through provider/resource and path-helper cleanup |
| offloader closing flag | private boolean with idempotent transition to true | module process lifetime |
| unpublished signed URL | private local string, never shared state | generation through successful client close only |

For signed PUT, `required_headers` is exactly
`{'content-type': content_type, 'x-goog-content-length-range':
'1,<max_upload_bytes>', 'x-goog-if-generation-match':
str(if_generation_match)}`. This is the three-header client contract, not the
SDK `headers` argument. The SDK receives `content_type=content_type` separately
and exactly
`headers={'x-goog-content-length-range': '1,<max_upload_bytes>',
'x-goog-if-generation-match': str(if_generation_match)}`. The signed request
carries no arbitrary query parameter and does not use a JSON API
generation-query spelling.

The exact per-command detail schemas are normative in R4–R7 and R9–R10.
Adding a field is a public-contract change and requires spec review.

## Component Interfaces

### Registry and boundary

```text
protocol_mapping['GCS'] -> GcsRequest
PROTOCOL_SCHEME_ALLOWLISTS['GCS'] -> frozenset({'gs'})
GcsRequest.validate_protocol_info(info, protocol='GCS') -> fresh dict
  Raises: ConfigurationError before envelope/preprocessor for strict info
```

### Strategy

```text
GcsRequest(url, auth, response, info, redact_params=...)
  Inputs: scheme-guarded target, strict config, raw auth, shared envelope
  Output: same GatewayResponse object finalized on success
  Raises: typed configuration/path/transport/GCS_STATUS errors;
          CancelledError and programming defects propagate
```

The existing entrypoint resolves the selector and validates URL type plus
strict `protocol_info`, creates the envelope, runs the preprocessor, and then
applies its GCS scheme guard. Without adding a shared hook,
`GcsRequest.__init__` validates the remaining target shape and `auth is None`
before delegating to `BaseRequestClass.__init__`, whose constructor performs
the breaker lookup. This is the earliest feasible current seam and guarantees
that invalid GCS scheme, remaining target shape, or auth reaches neither the
breaker registry nor ADC/local or remote I/O.

### Off-loop adapter

The module owns narrow typed private-pool helpers for ADC/refresh, client and
provider-resource construction/lookup, metadata reload, inclusive ranged
downloads, other storage operations, provider-resource/client close,
impersonation/IAM signing, and V4 URL generation. These helpers receive only
validated primitives, apply each exposed finite SDK timeout and the outer
result-acceptance deadline, return normalized data or private structured
failures, and never log or stringify credential or signed URL material. A
drained unkillable provider worker may extend observed wall-clock latency
beyond the deadline. Local filesystem work is not added to these helpers:
`read_guarded_file` and `stream_to_path` are awaited unchanged under the
lifecycle lease and keep their established internal default-executor use.

### Private GCS offloader

```text
_GCS_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix='asyncio-gateway-gcs',
)
_GCS_PERMITS = threading.BoundedSemaphore(4)

acquire lifecycle lease without waiting
  -> submit at most one private provider executor future
  -> shield/accept-or-drain that future
  -> submit the next provider future only after the prior future finishes
  -> await unchanged bounded path helpers under the retained lease as needed;
     their own default-executor work never enters _GCS_EXECUTOR
  -> close every owned provider resource through the retained lease
  -> release only when provider futures/resources and path helpers finish

_shutdown_gcs_offloader()
  -> atomically close admission (idempotent)
  -> once active leases == 0, executor.shutdown(
       wait=False, cancel_futures=True) exactly once
```

The executor, semaphore, lease object, shutdown function, and 64-KiB range cap
are private implementation details in `gcs_client.py`. No public constructor,
request option, environment variable, or shared helper exposes them. Admission
failure raises `GcsCapacityError` locally before local/credential/SDK work and
before breaker execution. A cancelled waiter retains its lease until private
provider work, provider cleanup, and any path-helper work complete. Executor
shutdown is deferred until accepted requests release their leases, cannot kill
a running Python thread, and `wait=False` does not guarantee immediate process
exit. The private executor neither replaces nor prohibits the path helpers'
existing internal default executor.

### Generation-pinned download lifecycle

```text
first breaker attempt, while no pin exists
  -> blob.reload(retry=None, timeout=validated_timeout,
                 [if_generation_match=caller_generation])
  -> validate and freeze generation, size, etag, crc32c
  -> refuse pinned_size > max_response_bytes before local mutation
each attempt after pin
  -> offset = 0; do not reload or repin
  -> download_as_bytes(start=offset,
                       end=min(offset + 65536, pinned_size) - 1,
                       if_generation_match=pinned_generation,
                       raw_download=True,
                       retry=None, timeout=validated_timeout)
  -> require bytes and exact inclusive-range length; yield async chunk
  -> unchanged stream_to_path(..., advertised_bytes=pinned_size,
                              max_bytes=max_response_bytes)
  -> require final byte count == pinned_size
```

A transient pin failure may be retried before the pin exists. A transient
range failure retries the entire attempt at offset zero against the same pin;
404/412 generation failures and malformed range results abort. Replacement of
the latest object therefore cannot mix generations in one file. Returned
`crc32c` is the value observed during the pin reload, not a claim that ranged
bytes were checksum-verified. `raw_download=True` binds range lengths and the
cap to the pinned stored size, so content-encoded objects are written in their
stored compressed representation and are never transparently decompressed.

### SDK signed URL lifecycle

```text
open storage client
  -> select/refresh validated signing credentials
  -> exactly one gateway call to blob.generate_signed_url(
       expiration=timedelta(seconds=expires_in_seconds), ...)
     -> google-auth/IAM implementation may internally retry signing
     -> PUT only: content_type is a dedicated argument; SDK headers has
        exactly length-range + generation-match (two entries)
  -> hold returned URL in a private local variable
  -> shield/drain off-loop client close
  -> close success: atomically publish exact signed_url success details
  -> close failure/cancellation: discard/sanitize URL; publish no URL state
```

GET supplies no arbitrary signed headers/query parameters. PUT supplies the
dedicated SDK `content_type` and only the exact two-header SDK mapping above,
while successful public details return the separate exact three-header client
contract. Neither method supplies arbitrary or generation-related query
parameters, and both convert relative expiry through `timedelta`. The gateway
neither enters the breaker nor retries the SDK generation call, including
after a private URL has been produced. This is a direct SDK lifecycle, not a
custom signer abstraction.

### Error type

```text
GcsStatusError(message: str, status_code: int | None = None)
  code: 'GCS_STATUS'
  default status: 502

GcsCapacityError(message: str = safe_constant_message)
  base: AsyncGatewayError
  code: 'GCS_CAPACITY'
  fixed status: 503
  details: no target-derived or secret-bearing values
```

## State Management

- No persistent application state, database transaction, cache, or session is
  introduced.
- Operational calls use the existing bucket-scoped breaker registry. Only
  structurally retryable transport/service failures affect its state.
- Every request acquires one of four private GCS lifecycle leases before any
  provider work, bounded local path-helper work, or breaker execution. The
  request owns that lease across sequential private provider futures,
  unchanged path helpers, timeout/cancellation drain, and provider-resource
  close; saturation or closing fails promptly without queuing or touching
  breaker state. Path-helper internal default-executor futures neither consume
  private GCS workers nor relax lease ownership.
- Upload bytes are read once before breaker execution and reused with the same
  generation precondition across attempts.
- Download generation, size, `etag`, and `crc32c` are set once by the first
  successful reload and remain immutable across retries. Each range attempt
  starts at zero, and byte/file state remains isolated in the existing guarded
  atomic writer; cancellation follows its pre/post-commit linearization
  contract.
- Signed URL generation does not enter the breaker or gateway retry loop. One
  gateway invocation may contain credential-library IAM retries. The bearer
  string remains private local state while the still-open client is closed
  under shield/drain and is assigned atomically to successful details only
  after close succeeds.

## Implementation Steps

1. Record the approved contract, dependency, and 16-file plan, then close the
   independent architecture, EM, security, and test-planning gates before
   implementation. **Files:** none (gate evidence only).
2. Add RED registry, scheme, strict allowlist, target/auth, and validation-
   before-ADC tests; add the dependency metadata assertion, including exact
   page-token boundary cases. **Files:** `tests/logic/test_gcs_client.py`,
   `tests/test_entrypoint.py`, `tests/test_packaging.py`, `pyproject.toml` (4).
3. Add `GcsStatusError`, `GcsCapacityError`, status-map entries, `GcsRequest`
   skeleton, registry, and scheme row sufficient to pass boundary tests.
   **Files:**
   `asyncio_gateway/logic/gcs_client.py`, `asyncio_gateway/logic/__init__.py`,
   `asyncio_gateway/asyncio_gateway.py`, `asyncio_gateway/utils/exceptions.py`,
   `asyncio_gateway/utils/status_map.py` (5).
4. Add RED provider-off-loop/deadline/retry-none/cleanup and capacity tests,
   including four blocked provider workers, prompt fifth/closing rejection,
   one outstanding private provider future per lease, retained cleanup
   capacity, cancellation drain, idempotent shutdown, exact safe telemetry,
   and proof that unrelated/default-executor work still schedules. Do not
   assert that the existing path helpers avoid their internal default executor.
   Then implement the private four-worker/four-permit provider adapter and
   shutdown path.
   **Files:**
   `tests/logic/test_gcs_client.py`, `asyncio_gateway/logic/gcs_client.py`,
   `tests/test_no_blocking_io.py` (3).
5. Add RED upload/download tests, then implement reuse of `read_guarded_file`
   and `stream_to_path`, guarded upload replay, and the download reload pin plus
   sequential inclusive stored-byte ranges capped at exactly 64 KiB, always
   passing `raw_download=True`. Cover exact arguments for one-byte/single-
   range, exact-64-KiB, multi-range, post-pin retry, generation-pinned
   replacement, gzip/content-encoding range/cap behavior, and rejection of a
   transparent-decompression-shaped overlong result, plus caller-generation
   authority, empty/cap boundaries, malformed metadata, 404, and 412 refusals.
   Keep both bounded path helpers unchanged and outside the private GCS
   provider pool while retaining the lifecycle lease across their calls.
   **Files:**
   `tests/logic/test_gcs_client.py`, `asyncio_gateway/logic/gcs_client.py` (2).
6. Add RED head/list tests, then implement one-page bounded normalization and
   safe page-token handling, including empty, exact 4096-byte, and 4097-byte
   UTF-8 cases plus exact whitespace preservation. **Files:**
   `tests/logic/test_gcs_client.py`, `asyncio_gateway/logic/gcs_client.py` (2).
7. Add RED direct/impersonated signed GET tests, then signed PUT tests for
   exact dedicated SDK `content_type`, exact two-entry SDK header mapping,
   separate exact three-entry public `required_headers`, no arbitrary signed
   query parameters, `timedelta` expiry conversion at 1/900/3600, one gateway
   `Blob.generate_signed_url` invocation, supported credential-internal retry
   semantics, close-before-publish, cleanup failure/cancellation, and
   exhaustive bearer containment; implement only after those tests fail.
   **Files:** `tests/logic/test_gcs_client.py`,
   `asyncio_gateway/logic/gcs_client.py` (2).
8. Extend global transport fixtures plus entrypoint, both GCS exception/status
   contracts, and provider-blocking/default-executor isolation contracts that
   preserve the path helpers' established internal behavior. **Files:**
   `tests/fixtures/protocol_transports.py`, `tests/test_entrypoint.py`,
   `tests/test_exceptions.py`, `tests/test_no_blocking_io.py` (4).
9. Extend documentation/package inventories and add the bounded GCS example.
   **Files:** `tests/test_docs.py`, `tests/test_packaging.py`,
   `examples/gcs_example.py` (3).
10. Update consumer/release documentation, then run the docs/example/package
    anti-drift checkpoint before proceeding. **Files:** `README.md`,
    `CHANGELOG.md` (2). The checkpoint executes existing tests without another
    file edit.
11. Run focused and full verification, security review, test review, dependency
   audit, clean artifacts, and acceptance review. Resolve every
   Critical/High/Medium finding before delivery. **Files:** none unless a
   failing gate returns work to its owning step.

Each implementation step is independently verifiable, touches at most five
files, and depends on no later step. Together the editing steps use exactly the
approved 16-file inventory and no other file.

## Error Handling

| Failure | Public handling | Retry/breaker |
|---|---|---|
| invalid selector, URL type, or protocol options—including non-string, empty, unencodable, or >4096-byte UTF-8 `page_token` | escaping `ConfigurationError` before envelope/preprocessor/ADC/breaker/SDK | no/no |
| invalid GCS scheme, remaining target shape, or auth | escaping `ConfigurationError` after preprocessor: scheme guard first, then `GcsRequest.__init__`; both before breaker/ADC/I/O | no/no |
| fifth lease or closing admission | envelope `GCS_CAPACITY`/503 from `GcsCapacityError`, with a constant safe message and no target/secret details | no/no |
| missing/incomplete ADC | envelope `CONFIG`/400 after contained discovery | no/no |
| unsafe local path or upload cap | existing `PATH` or `CONFIG` | no/no |
| advertised/observed download cap | existing `RESPONSE_TOO_LARGE` | no/no |
| DNS/TLS/connect/timeout | existing transport code/status | existing transport policy |
| GCS service 408/429/500/502/503/504 | `GCS_STATUS`, real valid status | yes/yes |
| generation-related 404/412 while pinning or reading a pinned download | `GCS_STATUS`, real 404/412; never repin | no/no |
| every other GCS service status | `GCS_STATUS`, real valid status | no/no |
| malformed pin metadata, non-bytes/short/overlong range (including transparent-decompression-shaped expansion), inconsistent final count, or other malformed nominal-success SDK data | `GCS_STATUS`/502; temporary download removed and existing target preserved | no/no |
| direct credentials cannot sign | safe `CONFIG`/400 | no/no |
| impersonation/IAM signing service refusal | safe `GCS_STATUS`, valid status or 502; no URL-bearing state | no gateway retry/no breaker; credential implementation may retry internally within the one invocation |
| cancellation | original `CancelledError` after retained-lease drain/cleanup; discard any unpublished private URL; atomic writer exception applies | no/no |
| cleanup-only failure | mapped typed failure without secret values; discard any private URL before returning | no/no |
| programming defect | propagates per existing one-conversion invariant | no/no |

No public error message includes credential-provider internals, tokens, signer
payloads, signatures, page tokens, signed URLs, arbitrary provider objects, or
unredacted embedded URLs.

## Edge-Case Implementation Map

- Empty/list-only object path: command-aware target validation.
- Numeric boundaries: shared explicit validators rejecting booleans,
  non-finite numbers, zero where positive is required, and values above caps.
- Concurrent object modification: generation preconditions; create-only
  upload and signed PUT default to generation `0`; download freezes one
  reload-observed generation and conditions every range on it.
- File identity races: existing held-descriptor guarded upload operation.
- Partial/interrupted download: exact sequential 64-KiB inclusive ranges feed
  the existing temporary-file and atomic-replace writer; retry restarts byte
  zero against the same pin and cancellation follows writer linearization.
- Content-encoded object: every range sets `raw_download=True`, so pinned size,
  range bounds, cap accounting, and destination bytes all describe the stored
  representation; transparent-decompression-shaped overlong output fails
  closed before commit.
- Latest-object replacement during download: generation 404/412 aborts without
  repinning, so bytes from different generations cannot share a destination.
- Ranged-read checksum expectations: pinned `crc32c` is metadata only; docs and
  tests make no gateway integrity-verification claim.
- Blocking provider SDK/ADC/IAM work: every remotely blocking provider seam is
  wrapped in named private-pool helpers and covered by thread-identity plus AST
  tests. The unchanged bounded local path helpers keep their established
  internal default-executor offloading while the lifecycle lease is held.
- Worker survives timeout/cancellation: reject its late result, shield, drain,
  close late resources, then re-raise the original outcome; wall-clock return
  latency may exceed the acceptance deadline.
- Malformed provider response: normalize from a closed schema or fail
  `GCS_STATUS`/502; never best-effort-drop fields.
- Signed bearer disclosure: build details only after all fallible work and
  scan every diagnostic surface in tests.
- Paging explosion: `max_items<=1000`, one page, explicit next token only.
- Caller page-token abuse: strict preprocessor-free validation rejects empty,
  unencodable, and >4096-byte UTF-8 values; valid values, including whitespace,
  reach the SDK unchanged and are never echoed/logged.
- GCS saturation/closing: a nonblocking lifecycle admission refuses the fifth
  or closing request with `GCS_CAPACITY` before local/ADC/breaker work; four
  retained leases guarantee cleanup submission for accepted requests.
- Shutdown during active work: closing admission is linearized and idempotent;
  running threads remain drained under their leases even though process exit
  may wait.

## Non-Functional Requirements

- **Performance:** event-loop blocking is forbidden; list is capped at 1000;
  caller page tokens are capped at 4096 UTF-8 bytes; transfers are byte-capped;
  only one page is read; download ranges are sequential and capped at exactly
  64 KiB of stored/raw data with transparent decompression disabled; and
  operational storage calls have no nested SDK retries. Exactly
  four private GCS lifecycle leases admit work without waiting, and each
  permits only one outstanding private provider future. Provider work is
  isolated from asyncio's default executor; the unchanged bounded
  `read_guarded_file` and `stream_to_path` primitives retain their established
  internal default-executor implementation under the held lease.
  Credential-internal IAM signing retries remain a residual property of one
  signed-URL gateway invocation.
- **Security:** ADC/Workload Identity only, least privilege, generation
  preconditions, strict target/options, finite result-acceptance deadlines,
  SDK timeouts where exposed, safe error schemas,
  bearer containment, stored/raw download caps that cannot be bypassed by
  transparent decompression, no raw credentials, and optional tightly
  validated IAM impersonation.
- **Reliability:** one gateway retry owner for operational storage calls,
  bucket-scoped breaker, no gateway retry for signed URLs, acknowledged
  credential-internal IAM retry, immutable generation-pinned downloads,
  cancellation-safe retained-lease drain/cleanup, fail-fast isolated GCS
  provider capacity, unchanged bounded local helper semantics, atomic
  exact stored-byte downloads, guarded uploads, and stable typed errors.
- **Compatibility:** additive `GCS` selector, `GCS_STATUS`, and
  `GCS_CAPACITY`; no change to existing selector behavior, top-level request
  signature, or envelope keys.
- **Observability:** safe command/bucket/outcome/latency may be logged through
  existing facilities for ordinary operations; object keys, page tokens,
  signer identity, credential data, signed URLs, and signed query/header values
  are excluded. Capacity telemetry uses only the five event names and fields
  frozen in AC13.7: lease state/rejection, over-deadline-or-cancellation drain
  start/end/duration, and closing. Those events exclude bucket/target, tokens,
  signer, credentials, payload, URL, and signed values.
- **Testability:** deterministic doubles cover every external seam and prove
  four blocked private provider workers do not starve unrelated/default-
  executor work without forbidding the existing path-helper implementation;
  no live GCP or emulator is required.
- **Documentation:** exact public contract, least-privilege guidance, signing
  limitations, and runnable bounded examples stay under anti-drift tests.

## Verification Commands

Use the repository's actual commands:

```bash
python -m pip install -e '.[dev]'
pytest
flake8 .
python .github/scripts/check_suppressions.py
flake8 --select=E1,E2,E3,E501,W2,W3,W505,Q .
mypy asyncio_gateway
python -m compileall -q examples/
python -m pip install build twine
python -m build
twine check dist/*
```

Run exact `flake8 .` in a temporary clean worktree so local untracked toolkit
files do not contaminate the result. The repository intentionally has no
autoformatter; the narrowed flake8 invocation above is its exact format check.
Also run clean wheel and source-distribution install/import tests and
`pip check`. CI must cover Python 3.10–3.14,
shuffled-order suites, and both statement and branch coverage ratchets.

## Rollback

Revert the GCS feature commit(s) as one unit: remove `gcs_client.py`, its
focused test and example; remove the `GCS` registry/scheme plus `GCS_STATUS`
and `GCS_CAPACITY` error/status rows; remove
`google-cloud-storage>=3,<4`; and revert README, CHANGELOG, fixture, entrypoint,
packaging, docs, exception, and blocking-I/O inventory changes. Before an
in-process feature teardown, invoke the private idempotent offloader shutdown;
deployment rollback should restart the process because `wait=False` cannot
kill already-running threads. Rebuild wheel/sdist in a clean environment, run
the full verification suite, and confirm existing nine-selector behavior and
metadata are restored. No remote GCP state or database migration requires
rollback. Already issued signed URLs cannot be revoked by reverting code and
remain valid until expiry or an external IAM/object-generation intervention.

## Residual Risks

- The repository does not live-test Cloud Storage enforcement of the signed
  PUT length-range or generation-match header, permissions, or expiration.
- A successfully issued signed URL remains a bearer capability until expiry;
  code rollback does not revoke it.
- IAM impersonation expands the runtime trust boundary and depends on external
  `iam.serviceAccounts.signBlob` policy that this repository cannot verify.
- One gateway `Blob.generate_signed_url` invocation may issue multiple remote
  IAM signing requests because supported google-auth credential implementations
  internally retry selected failures. The gateway cannot observe or disable
  those retries without the explicitly out-of-scope custom signer.
- ADC discovery and signing are synchronous; draining prevents resource leaks
  but cannot forcibly terminate an already-running Python worker thread and
  may make wall-clock return latency exceed the configured timeout.
- Four long-running or unkillable synchronous provider calls can occupy every
  GCS lease.
  Further GCS calls then fail promptly with `GCS_CAPACITY`; this protects the
  event loop and unrelated default-executor protocols but does not restore GCS
  availability until the workers finish or the process is replaced.
- `ThreadPoolExecutor.shutdown(wait=False, cancel_futures=True)` cancels only
  work that has not begun. Running GCS threads may delay interpreter exit even
  after the private shutdown path has returned.
- A pinned object generation can be deleted or become unavailable between
  ranges. The operation safely aborts rather than repinning, but the caller
  receives no file even if a newer generation exists.
- Ranged `download_as_bytes` calls do not independently verify CRC. Returned
  `crc32c` is pinned metadata and must not be interpreted as a gateway-verified
  checksum of the assembled local file.
- CI deterministically proves every ranged SDK call passes `raw_download=True`
  and rejects decompression-shaped length mismatches, but it does not live-test
  Cloud Storage handling of every possible `Content-Encoding`. The public
  contract remains the exact stored/raw representation, never a transparently
  decompressed body.
- URL generation can succeed before client close fails or cancellation is
  observed. The URL is then discarded and never published, but the underlying
  signed capability may exist until its short expiry even though no caller
  received it.
- The new dependency and transitive native packages may increase resolver,
  wheel, and source-artifact build time across Python 3.10–3.14.

## RARV and Self-Critique

- **Reason:** Deliver a contract-first, bounded GCS strategy that fits the
  existing strategy/envelope/breaker/file-safety architecture while treating
  credentials and signed URLs as high-risk material.
- **Act:** Freeze the selector, exact allowlists, schemas, error/retry rules,
  generation-pinned raw ranged-download lifecycle, fixed private capacity,
  off-loop lifecycle, signing constraints, 16-file inventory, tests, rollback,
  and residual risks in this single specification before code.
- **Reflect:** The weakest requirement is signed PUT size enforcement because
  it is provider behavior not live-tested here; AC10.2/AC10.5 resolve this by
  testing the dedicated SDK `content_type`, exact two-entry SDK `headers`, and
  separate exact three-entry client `required_headers`—including
  `x-goog-if-generation-match`—with no arbitrary query parameter. Exact
  `timedelta` tests prevent relative seconds from becoming an absolute SDK
  timestamp. The riskiest assumption is that unkillable synchronous ADC/SDK
  work eventually returns: R3 and R13 make retained-lease drain and late-
  provider-resource disposal testable, isolate provider work from the default
  executor, and state honestly that return/process-exit latency can exceed the
  deadline; they do not claim thread termination. The unchanged bounded local
  path helpers keep their established default-executor behavior while the
  lifecycle lease is held. The least testable criterion would have been
  “no secret leaks”; AC11.3, AC12.1, and AC13.7 make it finite by enumerating
  every owned surface/event/field and injecting sentinel secrets. The
  likeliest implementation failure is releasing a lease when the async waiter
  ends while its thread still runs, repinning after a range retry and mixing
  object generations, or omitting `raw_download=True` and allowing transparent
  decompression to invalidate pinned-size bounds. AC4.1–AC4.7 and
  AC13.2–AC13.8 require exact call traces, retained capacity, one-byte/64-KiB/
  multi-range/content-encoding adversarial doubles, replace-between-chunks
  tests, and retry from byte zero against one immutable stored-byte pin.
  AC3.1/AC3.6 additionally require thread-identity
  and AST proof that Google provider/credential work never consumes the event
  loop or default executor, without asserting that `read_guarded_file` or
  `stream_to_path` abandons its existing internal executor. The sequencing
  review is resolved by one normative order
  throughout: selector/URL-type/strict-info validation, envelope, preprocessor,
  GCS scheme guard, remaining target/auth validation, base breaker lookup,
  nonblocking capacity admission, then guarded local/ADC/breaker/SDK work. The
  signed-URL lifecycle keeps the SDK client open through exactly one gateway
  generation invocation, acknowledges credential-internal IAM retry, and
  publishes the private bearer only after shielded/drained close succeeds. The
  page-token criterion measures an exact 4096-byte UTF-8 ceiling without
  normalization, and its 4096/4097-byte boundary is directly testable.
  Cross-cutting work remains split into tasks of at most five files without
  expanding the 16-file inventory.
- **Verify:** Every R1–R13 requirement has individually numbered boolean
  acceptance criteria—71 unique AC IDs in total—edge cases, an implementation
  approach, and one of 13 traceability rows. No placeholder or unresolved open
  question remains. The plan matches the audited repository seams and remains
  within 16 files.

Severity review at specification stage: Critical—none; High—bearer disclosure,
IAM trust expansion, and dependency addition are contained; contract approval
is recorded and the independent security/planning gates remain mandatory;
Medium—signed PUT enforcement boundary, isolated-capacity lifecycle,
off-loop cancellation, bounded generation-pinned ranges, retry replay, and
list caps are resolved into explicit acceptance tests; Low—least privilege,
expiry/revocation, fail-fast saturation, and build-time impacts are documented.
Implementation may not start until the independent architecture, EM, security,
and test-planning gates close.

## Spec Traceability

| Spec Req | Implementation approach | Files |
|---|---|---|
| R1 | Register one strategy/scheme, enforce gs target + ADC-only auth, add exact dependency | `logic/__init__.py`, `asyncio_gateway.py`, `gcs_client.py`, `pyproject.toml`, packaging tests |
| R2 | Command-aware strict validator returns a copied config before preprocessor/ADC, preserving valid page tokens exactly under a 4096-byte UTF-8 cap | `gcs_client.py`, focused and entrypoint tests, README |
| R3 | Named private-pool provider adapters with result-acceptance deadlines, SDK timeouts, retry-none, drain/cleanup and AST/thread tests; unchanged path helpers retain internal default-executor use | `gcs_client.py`, `test_gcs_client.py`, `test_no_blocking_io.py` |
| R4 | Pin generation/stored size once, require `raw_download=True` on exact conditioned <=64-KiB ranges, reject decompression-shaped mismatches, feed raw bytes to unchanged atomic `stream_to_path`, and never repin | `gcs_client.py`, `test_gcs_client.py` |
| R5 | Await unchanged `read_guarded_file` under the lease outside the private provider pool, replay bytes with stable generation precondition, and normalize upload | `gcs_client.py`, `test_gcs_client.py` |
| R6 | One off-loop metadata fetch and closed-schema JSON normalization | `gcs_client.py`, `test_gcs_client.py` |
| R7 | One bounded page, exact unnormalized <=4096-byte caller token, explicit next token, and exact ordered item/page schemas | `gcs_client.py`, `test_gcs_client.py` |
| R8 | Private structural retry/abort failures convert to public `GCS_STATUS`; local saturated/closing admission converts to non-retryable `GCS_CAPACITY`/503 | `gcs_client.py`, `exceptions.py`, `status_map.py`, exception/focused tests |
| R9 | Direct Signing credentials or validated IAM impersonation use one gateway V4 GET invocation with exact relative `timedelta` expiry, then close-before-publish | `gcs_client.py`, `test_gcs_client.py`, README/example |
| R10 | Dedicated SDK content type + exact two SDK headers versus exact three client headers, `timedelta` expiry, no arbitrary query, one gateway invocation, internal-retry allowance, and cleanup-before-publish | `gcs_client.py`, `test_gcs_client.py`, README/example |
| R11 | Keep bearer private through shielded/drained close, atomically publish success only, and exhaustively scan failure surfaces | `gcs_client.py`, fixture, focused/invariant tests |
| R12 | Deterministic no-GCP tests include exact raw range calls and gzip/content-encoding adversaries; docs/example/package anti-drift and full quality/artifact gates cover the contract | all 16 inventoried files |
| R13 | Private four-worker/four-lease fail-fast provider offloader retains one lease through provider futures/resources and unchanged path helpers, with typed capacity refusal, idempotent shutdown, safe telemetry, and provider/default-executor isolation tests | `gcs_client.py`, `exceptions.py`, `status_map.py`, focused/exception/blocking-I/O tests, README |
