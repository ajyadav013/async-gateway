# Protocol Expansion Manifest

This manifest freezes the scope of the protocol-expansion program before
production code is changed. Any file or behavior not named here is out of
scope until the manifest is deliberately reviewed and amended.

## Program identity

- Branch: `codex/protocol-expansion`
- Base: `origin/master` at `11d6e26`
- Restore tag: `codex/protocol-expansion-base-20260820`
- Delivery mode: Enterprise Mode E (five independent lanes)
- Overall risk: High
- Human authorization: the user explicitly approved the recommended FTPS,
  JSON-RPC, and S3 additions, then explicitly expanded the request to GraphQL
  and gRPC and required tests for the whole program.
- Baseline: 3,129 passed, 8 skipped, 100% line and branch coverage; mypy and
  committed-tree flake8 clean.

## Frozen scope

The program adds exactly these capabilities:

1. JSON-RPC 2.0 single-request calls over HTTP(S), registered as `JSONRPC`.
2. GraphQL query/mutation calls over HTTP(S), registered as `GRAPHQL`, with
   body-level error semantics and partial-data preservation.
3. Explicit FTPS (`AUTH TLS`) as an opt-in mode of the existing `FTP`
   strategy, without changing legacy defaults.
4. A first-class `S3` strategy for download, upload, head, and one bounded
   list page.
5. Unary-unary gRPC calls, registered as `GRPC`, over explicitly insecure
   `grpc://` or TLS-protected `grpcs://` targets.

The program does not add REST aliases, JSON-RPC notifications or batches,
GraphQL subscriptions/multipart/file-upload transport, any gRPC streaming
shape/reflection/code generation/custom TLS or mTLS, S3 delete or arbitrary
SDK dispatch, session-token fields, live AWS calls, deployment, a pull
request, or a push. The only new dependency is the official `grpcio` runtime;
protobuf and `grpcio-tools` remain application/tooling concerns.

## Planned file inventory

### Add

- `asyncio_gateway/logic/jsonrpc_client.py`
- `asyncio_gateway/logic/graphql_client.py`
- `asyncio_gateway/logic/grpc_client.py`
- `asyncio_gateway/logic/s3_client.py`
- `tests/logic/test_jsonrpc_client.py`
- `tests/logic/test_graphql_client.py`
- `tests/logic/test_grpc_client.py`
- `tests/logic/test_s3_client.py`
- `tests/fixtures/grpc.py`, only if a local generic-bytes server is needed
- `examples/jsonrpc_example.py`
- `examples/graphql_example.py`
- `examples/grpc_example.py`
- `examples/s3_example.py`
- `docs/specs/protocol_expansion_manifest.md`
- `docs/specs/protocol_expansion_spec.md`
- `docs/specs/protocol_expansion_stories.md`

### Change: FTPS lane

- `asyncio_gateway/logic/ftp_client.py`
- `tests/logic/test_ftp_client.py`

### Change: shared protocol contract

- `asyncio_gateway/logic/__init__.py`
- `asyncio_gateway/asyncio_gateway.py`
- `asyncio_gateway/logic/http_client.py`, for one shared post-transport
  semantic-error hook used by the HTTP, JSON-RPC, and GraphQL strategies
- `asyncio_gateway/helpers/internal/base.py`, for selector-scoped option
  allowlists and destination identity where the current contract is
  insufficient
- `asyncio_gateway/helpers/internal/circuit_breaker_helper.py`, for proven
  botocore transport classification and the gRPC retryable/abortable status
  split
- `asyncio_gateway/utils/constants.py`
- `asyncio_gateway/utils/exceptions.py`
- `asyncio_gateway/utils/status_map.py`
- `asyncio_gateway/utils/http_file_config.py`
- `asyncio_gateway/utils/paths.py`, for a guarded, held-descriptor upload
  reader
- `pyproject.toml`, only to add verified `grpcio>=1.83.0,<2`

### Change: tests and public documentation

- `tests/fixtures/protocol_transports.py`
- `tests/test_entrypoint.py`
- `tests/test_entrypoint_invariant.py`
- `tests/test_envelope.py`
- `tests/test_docs.py`
- `tests/test_packaging.py`
- `tests/test_no_blocking_io.py`, only if its inventory requires an explicit
  new-module row
- `tests/logic/test_http_client.py`, for the shared semantic-error seam
- `tests/utils/test_http_file_config.py`
- `tests/utils/test_paths.py`
- `README.md`
- `CHANGELOG.md`, only for an Unreleased additive-feature note

The shared files above are serialization points: one worker owns them at a
time. Discovery of an unlisted required file is an `UNKNOWN` state and stops
that lane until this manifest is reviewed. Existing untracked project-tooling
files and unrelated worktrees are never staged or modified.

## Risk-ordered waves

### Wave 0: contract freeze

- Complete this manifest and the specification.
- Run Senior Backend, Technical Architecture, Engineering Management, and
  Devil's Advocate reviews.
- Break approved requirements into traceable implementation stories.

### Wave 1: JSON-RPC (Medium)

- Write focused tests and prove the RED state.
- Implement the `JSONRPC` strategy and its local registry/contract changes.
- Gate on focused tests plus the full regression suite.

### Wave 2: GraphQL (Medium)

- Write GraphQL request/error/partial-data tests and prove RED.
- Add the `GRAPHQL` semantic adapter without a GraphQL parser dependency.
- Gate on GraphQL-focused tests plus the full regression suite.

### Wave 3: explicit FTPS (High)

- Write downgrade-prevention and compatibility tests and prove RED.
- Add `tls_mode` without changing calls that omit it.
- Gate on FTP-focused tests plus the full regression suite.

### Wave 4: S3 (High)

- Write SDK-double, path-safety, size-cap, credential-redaction, and service
  failure tests and prove RED.
- Implement only the four allowlisted operations.
- Gate on S3-focused tests plus the full regression suite.

### Wave 5: unary gRPC (High)

- Add `grpcio` only after its registry/API/license evidence is captured.
- Write local generic-server, TLS-selection, serializer, metadata, status-map,
  cancellation, cleanup, and breaker tests and prove RED.
- Implement unary-unary calls only, then gate on focused and full suites.

### Final wave: integration and documentation

- Serialize shared registry, fixture, invariant, example, and README updates.
- Run independent code, contract, coverage, security, operability, and
  acceptance reviews.
- Run the exact final verification commands in a clean worktree.

## Execution limits

- At most one implementation worker and one gate runner per wave.
- At most two active delegated agents besides the root coordinator.
- Each implementation worker receives disjoint ownership and must not revert
  concurrent or user changes.
- No live credentials, buckets, endpoints, or destructive remote operation.
- No dependency, CI, packaging-metadata, or release-version change except the
  user-authorized `grpcio` runtime addition; any other change needs fresh
  approval.

## Required gates

A gate passes only with zero open Critical, High, or Medium findings and with
captured command or file/line evidence.

1. Spec complete
2. EM approved after architecture and adversarial plan review
3. Code review passed
4. Build green (lint and types)
5. Contract clear
6. Test coverage verified at 100% line and branch
7. Security clear, including TLS downgrade and credential-redaction checks
8. Pipeline green or explicitly skipped as non-deployable library work
9. Observability ready or explicitly skipped with evidence
10. Acceptance passed

## Rollback

Every lane is additive and can be reverted independently before the shared
registry/docs wave. The whole program can be compared with the immutable
restore tag `codex/protocol-expansion-base-20260820`; rollback is by reverting
the feature commits, never by resetting the user's working tree. No remote
state is created by the test plan.
