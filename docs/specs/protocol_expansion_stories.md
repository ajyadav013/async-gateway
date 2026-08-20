# Protocol Expansion Implementation Stories

This ledger converts every approved requirement in
`protocol_expansion_spec.md` into an ordered, test-first delivery story. A
story is complete only when its focused tests first demonstrate RED, its
implementation turns them GREEN, the full suite remains green at 100% line
and branch coverage, and its listed files contain no unrelated change.

## Execution rules

- Run stories in the dependency order below. Only one implementation story
  owns shared files at a time.
- Before production code in each lane, add the focused tests and retain the
  failing command output in the handoff notes. A collection/import failure is
  not acceptable RED evidence unless the missing module is the behavior under
  test; prefer a behavioral assertion failure.
- After focused GREEN, run `.venv/bin/pytest -q`. Stop on any regression.
- Existing untracked agent/tooling files are never staged. Stage paths
  explicitly.
- No live AWS credentials, remote bucket, external gRPC service, push, PR,
  release, or deployment is part of these stories.

## Dependency graph

```text
PE-00
  -> PE-10 -> PE-20 -> PE-30 -> PE-40 -> PE-50 -> PE-60
                                                    -> PE-70 -> PE-80
```

The serial order is intentional: `PE-10`, `PE-20`, and `PE-70` touch shared
dispatcher/contract files; `PE-40` and `PE-50` share the S3 filesystem helper;
`PE-60` owns the only dependency change. Focused test drafting for a later
lane may run in parallel only when it writes none of the active story's files.

## PE-00 — Approved contract checkpoint

- **Depends on:** none
- **Risk:** High program gate
- **Status:** complete when manifest/spec reviews are recorded
- **Owns:** `docs/specs/protocol_expansion_manifest.md`,
  `docs/specs/protocol_expansion_spec.md`, this ledger
- **Traceability:** all R1-R13 and AC1.1-AC13.6
- **Acceptance:** Senior Backend, Architecture, EM, and adversarial reviews
  have zero open Critical/High/Medium findings; branch and restore tag match
  the manifest; baseline is 3,129 passed and 8 skipped at 100% coverage.

## PE-10 — Shared boundary and semantic HTTP seam

- **Depends on:** PE-00
- **Risk:** High shared serialization point
- **Owns:** `asyncio_gateway/asyncio_gateway.py`,
  `asyncio_gateway/helpers/internal/base.py`,
  `asyncio_gateway/logic/http_client.py`, `asyncio_gateway/utils/exceptions.py`,
  `asyncio_gateway/utils/status_map.py`, `asyncio_gateway/utils/constants.py`,
  `tests/test_entrypoint.py`, `tests/test_exceptions.py`, and focused additions
  to `tests/logic/test_http_client.py`
- **Traceability:** R1/AC1.1-AC1.5, R4/AC4.5, R12, AC13.1
- **RED:** add tests for selector-scoped `None`, new-selector URL scheme rules,
  exact option allowlists/unknown rejection, stable new error codes, and the
  default HTTP status ordering plus overridable post-copy semantic hook.
  Run `.venv/bin/pytest -q tests/test_entrypoint.py tests/test_exceptions.py
  tests/logic/test_http_client.py -k 'protocol_option or semantic_hook or
  selector_payload or new_protocol_error'`.
- **GREEN:** add opt-in `ALLOWED_INFO_KEYS`, general selector scheme rules,
  legacy-safe payload normalization, error leaves/status rows, and one small
  HTTP response-error hook whose default exactly preserves HTTP behavior.
- **Regression gate:** focused command, then `.venv/bin/pytest -q`.

## PE-20 — JSON-RPC 2.0 single-call adapter

- **Depends on:** PE-10
- **Risk:** Medium
- **Owns:** `asyncio_gateway/logic/jsonrpc_client.py`,
  `tests/logic/test_jsonrpc_client.py`, and no registry/invariant files
- **Traceability:** R3/AC3.1-AC3.4, R4/AC4.1-AC4.5, R12 JSONRPC row,
  AC13.1-AC13.3
- **RED:** cover exact POST body, omitted `params`, id same-type equality,
  result/error exclusivity, malformed envelopes, RPC-error precedence over
  HTTP status, redirects disabled, owned session/serializer, retry count,
  caps, cancellation, redaction, and no-I/O validation. Run
  `.venv/bin/pytest -q tests/logic/test_jsonrpc_client.py`.
- **GREEN:** implement `JsonRpcRequest` over the PE-10 HTTP seam without a
  second exchange loop. Populate all response material before raising
  `JSONRPC_ERROR` or `JSONRPC_PROTOCOL`.
- **Regression gate:** focused command, then `.venv/bin/pytest -q`.

## PE-30 — GraphQL query/mutation adapter

- **Depends on:** PE-20
- **Risk:** Medium
- **Owns:** `asyncio_gateway/logic/graphql_client.py`,
  `tests/logic/test_graphql_client.py`
- **Traceability:** R8/AC8.1-AC8.3, R9/AC9.1-AC9.4, R12 GRAPHQL row,
  AC13.1-AC13.3
- **RED:** cover exact query/operationName/variables body, `None` omission,
  partial data with errors, error validation, both supported media types,
  frozen non-2xx precedence, owned session/serializer, redirect refusal,
  retries, cap, cancellation, and redaction. Run
  `.venv/bin/pytest -q tests/logic/test_graphql_client.py`.
- **GREEN:** implement the narrow semantic adapter on the shared HTTP seam;
  introduce no GraphQL parser/framework and preserve partial data on
  `GRAPHQL_ERROR`.
- **Regression gate:** focused command, then `.venv/bin/pytest -q`.

## PE-40 — Explicit FTPS compatibility lane

- **Depends on:** PE-30
- **Risk:** High TLS/downgrade boundary
- **Owns:** `asyncio_gateway/logic/ftp_client.py`,
  `tests/logic/test_ftp_client.py`, and only necessary additions to
  `tests/fixtures/ftp.py`
- **Traceability:** R2/AC2.1-AC2.6, AC13.1-AC13.2
- **RED:** prove legacy implicit/plaintext arguments are unchanged; explicit
  mode passes `ssl=None, upgrade_to_tls=True`; named modes reject
  `verify_ssl=False`; explicit rejects client certificates; invalid values
  make no connection; effective mode is reported. Run
  `.venv/bin/pytest -q tests/logic/test_ftp_client.py -k 'tls_mode or explicit
  or legacy_tls'`.
- **GREEN:** make the smallest constructor/context-call change; never add an
  unverified or fallback TLS path.
- **Regression gate:** full FTP file, then `.venv/bin/pytest -q`.

## PE-50 — Guarded S3 filesystem primitives and helper compatibility

- **Depends on:** PE-40
- **Risk:** High filesystem/atomicity boundary
- **Owns:** `asyncio_gateway/utils/paths.py`,
  `asyncio_gateway/utils/http_file_config.py`, `tests/utils/test_paths.py`,
  `tests/utils/test_http_file_config.py`
- **Traceability:** AC6.1-AC6.2, AC6.5-AC6.6, AC7.3, AC13.2-AC13.3
- **RED:** cover held no-follow upload descriptor, fallback inode/device
  equality, regular-file/cap validation, cancellation-safe close, exclusive
  download, legacy overwrite/uncapped defaults, failure-atomic temp replace,
  and before/during/after-commit cancellation. Run `.venv/bin/pytest -q
  tests/utils/test_paths.py tests/utils/test_http_file_config.py -k 's3 or
  safe_reader or atomic_replace'`.
- **GREEN:** add one guarded reader and one shared streamed download primitive.
  The overwrite path uses a same-directory temporary and the approved shielded
  atomic commit point; cleanup never removes the old target.
- **Regression gate:** both utility files, then `.venv/bin/pytest -q`.

## PE-60 — First-class S3 strategy

- **Depends on:** PE-50
- **Risk:** High credentials/retry/remote-status boundary
- **Owns:** `asyncio_gateway/logic/s3_client.py`,
  `tests/logic/test_s3_client.py`; if required, narrowly owns botocore
  classification additions in
  `asyncio_gateway/helpers/internal/circuit_breaker_helper.py`
- **Traceability:** R5/AC5.1-AC5.4, R6/AC6.1-AC6.6,
  R7/AC7.1-AC7.7, R12 S3 row, AC13.1-AC13.3
- **RED:** SDK doubles cover URI/auth/region/command validation, four success
  schemas, exact list normalization, opaque continuation token, body cleanup,
  service metadata, credential redaction, one-page bound, allowlisted transient
  vs abortable service failures, `total_max_attempts=1`, and exact `N+1`
  gateway calls. Run `.venv/bin/pytest -q tests/logic/test_s3_client.py`.
- **GREEN:** implement only download/upload/head/list with private service
  retry/abort types and no arbitrary SDK dispatch or live credential lookup in
  tests.
- **Regression gate:** focused command, then `.venv/bin/pytest -q`.

## PE-70 — Unary-unary gRPC runtime and adapter

- **Depends on:** PE-60
- **Risk:** High dependency/TLS/breaker boundary
- **Owns:** `pyproject.toml`, `asyncio_gateway/logic/grpc_client.py`,
  `tests/logic/test_grpc_client.py`, optional `tests/fixtures/grpc.py`, and the
  gRPC-only abortable-type addition in
  `asyncio_gateway/helpers/internal/circuit_breaker_helper.py`
- **Traceability:** R10/AC10.1-AC10.5, R11/AC11.1-AC11.5, R12 GRPC row,
  AC13.1-AC13.3
- **RED:** first add `grpcio>=1.83.0,<2`, reinstall the branch, then cover
  target/method/metadata/serializer validation, secure/insecure channel choice,
  receive cap, exact success/failure schema, base64/raw and deserialized paths,
  status map, metadata bounds/redaction, cleanup/cancellation, retryable
  statuses, and both shapes of uncounted `RESOURCE_EXHAUSTED`. Run
  `.venv/bin/pytest -q tests/logic/test_grpc_client.py`.
- **GREEN:** implement one per-call `grpc.aio` unary-unary channel, private
  retry/abort failures, deterministic status mapping, and no reflection,
  streaming, protobuf tooling, arbitrary options, or hidden plaintext.
- **Regression gate:** focused command, `pip check`, then `.venv/bin/pytest -q`.

## PE-80 — Registry, invariant, documentation, and acceptance integration

- **Depends on:** PE-70
- **Risk:** High shared integration point
- **Owns:** `asyncio_gateway/logic/__init__.py`,
  `tests/fixtures/protocol_transports.py`, `tests/test_entrypoint_invariant.py`,
  `tests/test_envelope.py`, `tests/test_docs.py`, `tests/test_packaging.py`,
  `tests/test_no_blocking_io.py` if needed, `README.md`, `CHANGELOG.md`, and the
  four protocol examples named by the manifest
- **Traceability:** R1/AC1.1-AC1.5, R12, R13/AC13.1-AC13.6, plus integration
  coverage for R2-R11
- **RED:** register the new strategy rows in contract fixtures/tests before
  the production registry and prove the matrix/docs tests fail. Run
  `.venv/bin/pytest -q tests/test_entrypoint_invariant.py tests/test_envelope.py
  tests/test_docs.py tests/test_packaging.py`.
- **GREEN:** add the four selectors, deterministic transport doubles, hostile
  invariants, runnable examples, exact option/error tables, retry warnings,
  S3 migration guidance, and an Unreleased changelog entry.
- **Acceptance:** run `.venv/bin/pytest -q`; `.venv/bin/mypy asyncio_gateway`;
  `.venv/bin/flake8 asyncio_gateway tests examples`; security/dependency/secret
  reviews; build wheel+sdist and install/smoke them; then create a temporary
  clean worktree at the final commit and run exact `.venv/bin/flake8 .` there.
  Required verdict: 100% line and branch coverage, zero lint/type/build/test/
  security findings, no live external calls, and zero staged unrelated files.

## Traceability audit

| Requirement | Stories |
|---|---|
| R1 common registration/contract | PE-10, PE-80 |
| R2 FTPS | PE-40 |
| R3-R4 JSON-RPC | PE-10, PE-20 |
| R5-R7 S3 | PE-50, PE-60 |
| R8-R9 GraphQL | PE-10, PE-30 |
| R10-R11 gRPC | PE-70 |
| R12 option inventories | PE-10, PE-20, PE-30, PE-60, PE-70, PE-80 |
| R13 tests/quality | every implementation story; final proof in PE-80 |

No approved requirement or acceptance criterion is intentionally deferred.
