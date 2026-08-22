# GCS Selector — Story Breakdown and Coverage Gate

**Source:** [approved GCS selector specification](gcs_selector_spec.md) (R1–R13; 71 acceptance criteria)
**Planning mode:** Mode A, one backend lane. This document is planning only: it authorizes no implementation, live GCP access, deployment, push, or scope expansion.

## Boundaries and assumptions

- The product-file inventory is frozen to the 16 paths in the source specification. No story may add a public option, command, file, dependency, CI/deployment change, live-GCP test, or release action.
- Every implementation story is **RED → GREEN → refactor → focused verification**. “RED” means the named deterministic test or contract assertion is committed/run first and demonstrably fails because its corresponding behavior is absent; production behavior may be added only after that evidence. The story may not be called done until its focused tests are green.
- `gcs_client.py` and `tests/logic/test_gcs_client.py` are shared critical files. Mode A therefore permits only the two explicitly disjoint foundations below to start together. All other implementation stories are deliberately serial, even where their functional requirements might otherwise look independent.
- This planner owns only this document. `.claude/CONTINUITY.md` must be updated by the parent/orchestrator at handoff with this coverage result; editing it here would violate the one-output-file assignment.

## Frozen product inventory (exactly 16 files)

1. `asyncio_gateway/asyncio_gateway.py`
2. `asyncio_gateway/logic/__init__.py`
3. `asyncio_gateway/logic/gcs_client.py`
4. `asyncio_gateway/utils/exceptions.py`
5. `asyncio_gateway/utils/status_map.py`
6. `examples/gcs_example.py`
7. `tests/fixtures/protocol_transports.py`
8. `tests/logic/test_gcs_client.py`
9. `tests/test_docs.py`
10. `tests/test_entrypoint.py`
11. `tests/test_exceptions.py`
12. `tests/test_no_blocking_io.py`
13. `tests/test_packaging.py`
14. `CHANGELOG.md`
15. `README.md`
16. `pyproject.toml`

Planning and ticket documents are not product scope. A repeated file in successive stories remains a shared-file handoff, not an inventory expansion.

## Stories

### GCS-01 — Package the supported Google Storage dependency

**Goal:** Make the exact approved runtime dependency visible to installation and packaging checks.

**Why:** The selector cannot be imported reliably, or distributed safely, without the frozen `google-cloud-storage>=3,<4` package contract.

**Acceptance IDs:** AC1.4, AC12.1, AC12.3, AC12.4.

**Product-file boundary (2):**

- `tests/test_packaging.py`
- `pyproject.toml`

**RED evidence before production:** Add focused packaging assertions that fail until runtime metadata contains exactly `google-cloud-storage>=3,<4` and clean package/import inventory recognizes the selector dependency; run the focused packaging test and capture the failure.

**GREEN/refactor/verification:** Add only the approved metadata entry; keep no emulator or extra dependency. Re-run the focused packaging test, then the relevant install/import/build checks prescribed by the spec.

**Done checks:** Exact dependency range present; no new dependency; focused packaging assertion green; file count remains 2.

**blockedBy:** none
**blocks:** GCS-02, GCS-09

---

### GCS-02 — Establish the strict public selector boundary

**Goal:** Register `GCS` and implement its strict public validation/order through the existing entrypoint and request constructor.

**Why:** Invalid selector, scheme, target, auth, and command options must be rejected at their mandated boundary before any cloud or local side effect.

**Acceptance IDs:** AC1.1, AC1.2, AC1.3, AC1.5, AC2.1, AC2.2, AC2.3, AC2.4, AC2.5, AC2.6, AC8.5, AC11.1, AC12.1, AC12.2, AC12.3.

**Product-file boundary (5):**

- `asyncio_gateway/asyncio_gateway.py`
- `asyncio_gateway/logic/__init__.py`
- `asyncio_gateway/logic/gcs_client.py`
- `tests/logic/test_gcs_client.py`
- `tests/test_entrypoint.py`

**RED evidence before production:** First add deterministic entrypoint/client tests asserting: case-insensitive `GCS` registry resolution and exact `{'gs'}` allowlist; selector/URL-type/strict-info errors before envelope/preprocessor; post-preprocessor non-`gs` scheme guard; target/auth validation before breaker/ADC/local I/O; bucket-only breaker destination; fresh copied command-specific options; command, numeric, page-token (empty, whitespace, 4096/4097 UTF-8 bytes, encoding failure), and signed-method boundaries. Run the focused tests while GCS is absent/skeletal and record failures.

**GREEN/refactor/verification:** Register only GCS, add the GCS-to-`gs` dispatch row, and add the smallest strict `GcsRequest` boundary/validator satisfying those tests. Preserve existing selector behavior and the top-level envelope keys.

**Done checks:** All named boundary tests green; no ADC, path helper, breaker, or SDK double observed on rejected input; exactly five files or fewer; no public surface beyond the specification.

**blockedBy:** GCS-01, GCS-03
**blocks:** GCS-04

---

### GCS-03 — Introduce stable typed GCS error vocabulary

**Goal:** Add the two approved typed errors and their central status classifications.

**Why:** Later lifecycle and service behavior needs stable, safe `GCS_STATUS` and `GCS_CAPACITY` envelope vocabulary without leaking provider objects.

**Acceptance IDs:** AC8.1, AC13.4, AC12.1, AC12.3.

**Product-file boundary (3):**

- `asyncio_gateway/utils/exceptions.py`
- `asyncio_gateway/utils/status_map.py`
- `tests/test_exceptions.py`

**RED evidence before production:** Add focused hierarchy/status tests that fail until `GcsStatusError(ProtocolError)` supplies `GCS_STATUS`/502 and warning-class remote mapping, and `GcsCapacityError(AsyncGatewayError)` supplies `GCS_CAPACITY`/503 with local error-level classification and safe constant-only details.

**GREEN/refactor/verification:** Implement only those two types and map entries. Do not introduce conversion policy, capacity admission, or new error surfaces in this story.

**Done checks:** Focused exception tests green; no provider text/target/secret belongs to capacity error; three-file limit met.

**blockedBy:** none
**blocks:** GCS-02, GCS-04, GCS-08

---

### GCS-04 — Build the isolated provider lifecycle and capacity guard

**Goal:** Provide the private four-worker/four-lease offloader, deadline/drain/cleanup semantics, and generic GCS failure conversion needed by all operations.

**Why:** Google SDK, ADC, credential, IAM, and client work must not block or consume the default executor, and stalled work must not starve unrelated protocols.

**Acceptance IDs:** AC3.1, AC3.2, AC3.3, AC3.4, AC3.5, AC3.6, AC8.2, AC8.3, AC8.4, AC8.5, AC8.6, AC11.1, AC12.1, AC12.2, AC12.3, AC13.1, AC13.2, AC13.3, AC13.4, AC13.5, AC13.6, AC13.7, AC13.8.

**Product-file boundary (3):**

- `asyncio_gateway/logic/gcs_client.py`
- `tests/logic/test_gcs_client.py`
- `tests/test_no_blocking_io.py`

**RED evidence before production:** Add deterministic executor/lease doubles and focused contract tests that fail until they prove: exact private executor identity/configuration; no provider seam/default-executor submission; nonblocking four-lease admission and prompt fifth/closing `GCS_CAPACITY`; one provider future per retained lease; finite SDK timeout and result-acceptance deadline; shielded draining/resource close under timeout/repeated cancellation; exact safe telemetry; idempotent deferred shutdown; structural service/transport mapping and retry/breaker classification. The tests explicitly permit unchanged `read_guarded_file` and `stream_to_path` internal default-executor use.

**GREEN/refactor/verification:** Implement private lifecycle helpers, never a shared offloader or public tuning surface. Verify no provider result becomes success after deadline, cleanup never replaces original failure/cancellation, and no operational SDK retry will be enabled by later command stories.

**Done checks:** Focused lifecycle/AST tests green, thread identity captured for every provider seam, blocked four-worker test still schedules unrelated default-executor work, all secrets absent from telemetry; three-file limit met.

**blockedBy:** GCS-02, GCS-03
**blocks:** GCS-05

---

### GCS-05 — Deliver guarded upload and generation-pinned raw download

**Goal:** Add the two bounded local-file transfer commands using the lifecycle guard, unchanged path helpers, and exact stored/raw GCS semantics.

**Why:** Transfers carry the largest data-integrity risk: uploads must not reopen/race local files, and downloads must not mix generations or transparently decompress beyond the cap.

**Acceptance IDs:** AC3.3, AC3.4, AC3.5, AC4.1, AC4.2, AC4.3, AC4.4, AC4.5, AC4.6, AC4.7, AC4.8, AC5.1, AC5.2, AC5.3, AC5.4, AC8.3, AC8.4, AC8.6, AC11.1, AC11.2, AC12.1, AC12.2.

**Product-file boundary (2):**

- `asyncio_gateway/logic/gcs_client.py`
- `tests/logic/test_gcs_client.py`

**RED evidence before production:** First add focused upload/download tests that fail until they assert: one guarded read before breaker with replayed bytes/precondition; reload pin validation and caller generation authority; 64-KiB sequential inclusive `download_as_bytes` calls with pinned generation, `raw_download=True`, `retry=None`, and finite timeout; empty/one-byte/exact-64-KiB/multi-range behavior; stored compressed bytes; advertised cap before path mutation; post-pin retry starts at zero without reload/repin; 404/412 abort; malformed/non-bytes/short/overlong/decompression-shaped ranges fail closed; atomic destination behavior and exact success detail schemas.

**GREEN/refactor/verification:** Reuse—not modify—`read_guarded_file` and `stream_to_path` while retaining the lease. Disable SDK retries at each operational call and preserve original cancellation/atomic-writer semantics.

**Done checks:** Exact-call doubles green; existing target survives every pre-commit failure; success file equals stored/raw bytes; no checksum-verification claim; two-file limit met.

**blockedBy:** GCS-04
**blocks:** GCS-06

---

### GCS-06 — Deliver normalized head and one-page list

**Goal:** Add bounded metadata and single-page listing with closed JSON-safe schemas and opaque page-token containment.

**Why:** Consumers need stable metadata and pagination without receiving SDK types, recursively enumerating a bucket, or disclosing caller tokens.

**Acceptance IDs:** AC3.1, AC3.3, AC3.4, AC6.1, AC6.2, AC6.3, AC7.1, AC7.2, AC7.3, AC7.4, AC8.2, AC8.3, AC8.4, AC8.6, AC11.1, AC11.2, AC11.4, AC12.1, AC12.2.

**Product-file boundary (2):**

- `asyncio_gateway/logic/gcs_client.py`
- `tests/logic/test_gcs_client.py`

**RED evidence before production:** Add focused head/list tests that fail until they assert one off-loop `retry=None` finite-timeout metadata fetch; exact normalized head schema/type refusal; one-page construction/fetch/iteration/close only; exact unchanged caller token reaches SDK (including non-empty whitespace and 4096-byte token); service-order items, exact list schema, no recursive next-page following, malformed item/page failure, and no caller-token echo/logging.

**GREEN/refactor/verification:** Implement only head and a single bounded list page on the existing private lifecycle. Normalize every declared scalar/mapping; do not add pagination APIs.

**Done checks:** Focused tests green; `max_items` remains 1..1000; list prefix accepts empty target only for list; all returned schemas contain no SDK/page/iterator object; two-file limit met.

**blockedBy:** GCS-05
**blocks:** GCS-07

---

### GCS-07 — Deliver constrained V4 signed GET and PUT without bearer leakage

**Goal:** Generate exactly one bounded signed URL for GET or PUT, with direct/impersonated signing, close-before-publication, and exact headers/details.

**Why:** A signed URL is a bearer secret; issuance must have no gateway retry/breaker path and must not leak an unpublished URL or credentials.

**Acceptance IDs:** AC3.1, AC3.2, AC3.4, AC9.1, AC9.2, AC9.3, AC9.4, AC9.5, AC10.1, AC10.2, AC10.3, AC10.4, AC10.5, AC11.1, AC11.2, AC11.3, AC11.4, AC11.5, AC12.1, AC12.2.

**Product-file boundary (2):**

- `asyncio_gateway/logic/gcs_client.py`
- `tests/logic/test_gcs_client.py`

**RED evidence before production:** Add deterministic direct-signing and IAM-impersonation tests that fail until they assert: strict GET/PUT/method/expiry/service-account/content-type/PUT-cap validation; signer capability checks; exact `timedelta(seconds=value)` calls at 1/900/3600; one `Blob.generate_signed_url` gateway invocation with client open; GET without arbitrary headers/query; PUT dedicated `content_type`, exact two SDK headers, and separate exact three public `required_headers`; no breaker/gateway retry while allowing credential-internal IAM retry; shielded close before atomic publication; sentinel URL/credential absence in all failure, cancellation, log, trace, exception, and breaker surfaces.

**GREEN/refactor/verification:** Implement signed GET/PUT only through the existing lifecycle. Keep the URL in private local state until close succeeds; discard it if close fails or cancellation occurs before publication.

**Done checks:** Focused signing and containment suite green; every successful detail schema exact; no custom host/query/header/credential input; two-file limit met.

**blockedBy:** GCS-06
**blocks:** GCS-08

---

### GCS-08 — Extend shared deterministic contract matrices

**Goal:** Wire the completed selector into global fixtures and cross-module registry, error, and no-blocking-I/O invariants.

**Why:** The focused suite alone cannot prove that the new selector preserves the repository-wide protocol matrix and shared error/fixture contracts.

**Acceptance IDs:** AC1.1, AC1.4, AC8.1, AC11.1, AC12.1, AC12.2, AC12.3, AC13.4, AC13.7, AC13.8.

**Product-file boundary (4):**

- `tests/fixtures/protocol_transports.py`
- `tests/test_entrypoint.py`
- `tests/test_exceptions.py`
- `tests/test_no_blocking_io.py`

**RED evidence before production behavior:** Add/extend the global fixture and invariant assertions first: GCS registry/scheme/envelope rows; exception/status rows; provider-prefix AST/default-executor isolation; capacity status and exact safe telemetry rows. Run these tests against the completed implementation to identify any contract mismatch before altering shared test data.

**GREEN/refactor/verification:** Make the minimal fixture/assertion updates that accurately encode the already implemented public contract. This story adds no new runtime behavior and may not “fix” a red invariant by weakening it.

**Done checks:** Cross-module matrices green alongside focused GCS suite; existing selectors unchanged; four-file limit met.

**blockedBy:** GCS-03, GCS-07
**blocks:** GCS-09

---

### GCS-09 — Publish documented, example, and artifact-verified GCS contract

**Goal:** Add the runnable bounded example and consumer/release documentation, with anti-drift checks tied to package artifacts.

**Why:** The public selector is unsafe to ship unless its narrow command, signing, capacity, storage-byte, and no-live-GCP boundaries are discoverable and packaging/documentation remain synchronized.

**Acceptance IDs:** AC1.4, AC4.7, AC9.2, AC10.2, AC10.3, AC10.5, AC12.1, AC12.3, AC12.4, AC12.5, AC12.6.

**Product-file boundary (5):**

- `CHANGELOG.md`
- `README.md`
- `examples/gcs_example.py`
- `tests/test_docs.py`
- `tests/test_packaging.py`

**RED evidence before production behavior:** First extend documentation/package anti-drift tests so they fail until the README/example/CHANGELOG cover all five commands, exact strict allowlists and schemas, ADC/Workload Identity and least privilege, impersonation/signing limits, retry/timeout/capacity behavior, page-token rule, raw pinned download/no-CRC limitation, signed bearer/expiry limitation, and PUT’s two-SDK-vs-three-client headers/relative expiry. Add a compile/import assertion for the example and run the focused docs/packaging checks red.

**GREEN/refactor/verification:** Write only the approved documentation, additive changelog note, and bounded no-live-GCP example. Do not edit CI, create a release tag, deploy, or claim service-side enforcement was live tested.

**Done checks:** Anti-drift tests and example compilation green; complete focused suite and specified full quality/artifact commands are run by the downstream test/quality gates; no C/H/M issue remains; five-file limit met.

**blockedBy:** GCS-01, GCS-08
**blocks:** none

## Dependency graph

```text
GCS-01 ─┐
        ├─> GCS-02 ─> GCS-04 ─> GCS-05 ─> GCS-06 ─> GCS-07 ─> GCS-08 ─> GCS-09
GCS-03 ─┘                   ↑                                      ↑
                            └───────────────────────────────────────┘
```

More precisely: `GCS-02.blockedBy=[GCS-01,GCS-03]`; `GCS-04=[GCS-02,GCS-03]`; `GCS-05=[GCS-04]`; `GCS-06=[GCS-05]`; `GCS-07=[GCS-06]`; `GCS-08=[GCS-03,GCS-07]`; `GCS-09=[GCS-01,GCS-08]`. `GCS-01` and `GCS-03` have no prerequisites. The `blocks` fields in each story are the exact inverse edges.

## Immediate work and sequencing

**Immediately startable implementation set:**

- **Package lane:** GCS-01 — owns only `pyproject.toml` and `tests/test_packaging.py`.
- **Typed-error lane:** GCS-03 — owns only exception/status-map files and `tests/test_exceptions.py`.

These two sets are disjoint. They may run concurrently only if each lane preserves the other’s work. Every later story is a **single sequential backend lane** because it changes `gcs_client.py` and/or `test_gcs_client.py`, or it is a global-contract/documentation join that needs the completed runtime surface. Read-only review of the spec or test design can overlap, but no other implementation ticket is safely parallel.

Suggested order: GCS-01 + GCS-03 → GCS-02 → GCS-04 → GCS-05 → GCS-06 → GCS-07 → **Merge Reviewer join 1** (shared request/error/offloader contract) → GCS-08 → **Merge Reviewer join 2** (global fixture/entrypoint/error/no-blocking invariants) → GCS-09 → downstream test/security/artifact/acceptance gates.

## Mechanical AC traceability

Every acceptance ID from the approved source maps to one or more stories. Repetition is intentional for cross-cutting validation; there are no unowned stories.

| Requirement | Source acceptance IDs (defined) | Story coverage |
|---|---|---|
| R1 | AC1.1, AC1.2, AC1.3, AC1.4, AC1.5 | GCS-01 (AC1.4); GCS-02 (AC1.1–AC1.5); GCS-08 (AC1.1, AC1.4); GCS-09 (AC1.4) |
| R2 | AC2.1, AC2.2, AC2.3, AC2.4, AC2.5, AC2.6 | GCS-02 (AC2.1–AC2.6) |
| R3 | AC3.1, AC3.2, AC3.3, AC3.4, AC3.5, AC3.6 | GCS-04 (AC3.1–AC3.6); GCS-05 (AC3.3–AC3.5); GCS-06 (AC3.1, AC3.3, AC3.4); GCS-07 (AC3.1, AC3.2, AC3.4) |
| R4 | AC4.1, AC4.2, AC4.3, AC4.4, AC4.5, AC4.6, AC4.7, AC4.8 | GCS-05 (AC4.1–AC4.8); GCS-09 (AC4.7) |
| R5 | AC5.1, AC5.2, AC5.3, AC5.4 | GCS-05 (AC5.1–AC5.4) |
| R6 | AC6.1, AC6.2, AC6.3 | GCS-06 (AC6.1–AC6.3) |
| R7 | AC7.1, AC7.2, AC7.3, AC7.4 | GCS-06 (AC7.1–AC7.4) |
| R8 | AC8.1, AC8.2, AC8.3, AC8.4, AC8.5, AC8.6 | GCS-03 (AC8.1); GCS-02 (AC8.5); GCS-04 (AC8.2–AC8.6); GCS-05 (AC8.3, AC8.4, AC8.6); GCS-06 (AC8.2–AC8.4, AC8.6); GCS-08 (AC8.1) |
| R9 | AC9.1, AC9.2, AC9.3, AC9.4, AC9.5 | GCS-07 (AC9.1–AC9.5); GCS-09 (AC9.2) |
| R10 | AC10.1, AC10.2, AC10.3, AC10.4, AC10.5 | GCS-07 (AC10.1–AC10.5); GCS-09 (AC10.2, AC10.3, AC10.5) |
| R11 | AC11.1, AC11.2, AC11.3, AC11.4, AC11.5 | GCS-02 (AC11.1); GCS-04 (AC11.1); GCS-05 (AC11.1, AC11.2); GCS-06 (AC11.1, AC11.2, AC11.4); GCS-07 (AC11.1–AC11.5); GCS-08 (AC11.1) |
| R12 | AC12.1, AC12.2, AC12.3, AC12.4, AC12.5, AC12.6 | GCS-01 (AC12.1, AC12.3, AC12.4); GCS-02/GCS-04/GCS-05/GCS-06/GCS-07/GCS-08/GCS-09 (AC12.1); GCS-02/GCS-04/GCS-05/GCS-06/GCS-07/GCS-08 (AC12.2); GCS-01/GCS-02/GCS-03/GCS-04/GCS-08/GCS-09 (AC12.3); GCS-01/GCS-09 (AC12.4); GCS-09 (AC12.5, AC12.6) |
| R13 | AC13.1, AC13.2, AC13.3, AC13.4, AC13.5, AC13.6, AC13.7, AC13.8 | GCS-04 (AC13.1–AC13.8); GCS-03 (AC13.4); GCS-08 (AC13.4, AC13.7, AC13.8) |

## RARV coverage-gate verification

**Reason:** Decompose the approved 13-requirement, 71-AC library feature into verifiable test-first tickets without expanding the frozen 16-file scope.

**Act:** Created nine stories, each with a one-ticket boundary, at most five owned product files, named RED evidence, GREEN behavior, focused verification, dependencies, and done checks.

**Reflect:** The only safe parallel implementation is GCS-01/GCS-03 because their scopes are disjoint. The core strategy is otherwise intentionally serialized to prevent shared-file conflicts and partial API/lifecycle contracts. No story is documentation-only without acceptance coverage; no story adds excluded GCP/deploy/live-test scope.

**Verify (mechanical plan audit):**

| Check | Expected | Result |
|---|---:|---:|
| Unique source AC IDs | 71 | 71 defined |
| AC IDs mapped by this document | 71 | 71 covered; 0 missing |
| Unknown AC IDs in mappings | 0 | 0 |
| Stories with zero ACs | 0 | 0 |
| Graph cycles | 0 | 0 (topological order: 01/03, 02, 04, 05, 06, 07, 08, 09) |
| Maximum product files per story | 5 | 5 |
| Frozen inventory union | 16 | 16 exact; no extra product file |
| Immediately startable stories with unmet dependency | 0 | 0 (GCS-01, GCS-03 only) |

**Severity/gaps:** No Critical, High, Medium, Low, or Cosmetic planning gap found. The required `.claude/CONTINUITY.md` handoff update is intentionally not performed because the parent assigned this agent ownership of exactly one output file; the parent/orchestrator must record this RARV evidence there.
