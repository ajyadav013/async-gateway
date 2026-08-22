# AGW-52: Publish documented example and artifact-verified GCS contract

- **Status:** IN REVIEW
- **Branch:** `codex/gcs-selector`
- **Story:** GCS-09 — [GCS selector story plan](../../specs/gcs_selector_stories.md)
- **Spec:** [GCS selector specification](../../specs/gcs_selector_spec.md)
- **Decisions:** Bounded GCS decisions of record in [the GCS selector specification](../../specs/gcs_selector_spec.md); no ADR is required.
- **Acceptance criteria:** AC1.4, AC4.7, AC9.2, AC10.2, AC10.3, AC10.5, AC12.1, AC12.3, AC12.4, AC12.5, AC12.6
- **Files (declared scope):** `CHANGELOG.md`, `README.md`, `examples/gcs_example.py`, `tests/test_docs.py`, `tests/test_packaging.py`

## Why

The public selector is unsafe to ship unless its narrow command, signing,
capacity, storage-byte, and no-live-GCP boundaries are discoverable and
packaging/documentation remain synchronized (GCS-09).

## RED-first plan and definition of done

- **RED first:** Extend documentation/package anti-drift tests so they fail
  until README/example/CHANGELOG cover all five commands, exact strict
  allowlists and schemas, ADC/Workload Identity and least privilege,
  impersonation/signing limits, retry/timeout/capacity behavior, page-token
  rule, raw pinned download/no-CRC limitation, signed bearer/expiry limitation,
  and PUT’s two-SDK-vs-three-client headers/relative expiry. Add a compile/
  import assertion for the example and run focused docs/packaging checks red.
- **GREEN/refactor:** Write only the approved documentation, additive changelog
  note, and bounded no-live-GCP example. Do not edit CI, create a release tag,
  deploy, or claim service-side enforcement was live tested.
- **Done checks:** Anti-drift tests and example compilation green; complete
  focused suite and specified full quality/artifact commands are run by
  downstream test/quality gates; no C/H/M issue remains; five-file limit met.

## Work Log

- 2026-08-22 — Opened from approved GCS-09 planning before implementation;
  recorded its five-file boundary, acceptance criteria, RED-first proof, and
  dependency relation. Files: this ticket and the local ticket/wiki index.
- 2026-08-22 — Established the focused pre-correction RED baseline at 343
  collected: 318 passed, 23 failed, and 2 skipped. Only
  `tests/test_docs.py` was dirty; its README checks still searched outside a
  GCS section and packaging had no GCS example coverage.
- 2026-08-22 — Corrected the RED design without changing deliverables:
  bounded README assertions to one dedicated H2 GCS section, added exact
  SDK-versus-client header and command/schema checks, extended the existing
  example inventory and artifact-import gate, and added a non-`__main__`
  request/provider/network sentinel. Also updated the test-only
  `handle_request` inventory from seven to eight implementations.
- 2026-08-22 — Final tests-only RED collected 357 tests: 315 passed, 40
  failed, and 2 skipped. All failures identify missing or stale README,
  CHANGELOG, or `examples/gcs_example.py` deliverables; no test performed
  live GCP, ADC, metadata-server, emulator, DNS, or network work. Product
  changes remain limited to `tests/test_docs.py` and
  `tests/test_packaging.py`, and the RED remains intentionally uncommitted.
- 2026-08-22 — Applied two bounded candidate correction lanes after the
  failure review. The documentation lane's `tests/test_docs.py` SHA-256 is
  `e4d4771d976393dd4d11864c736b536ef955ef37ef1099da2622e9033bea0beb`;
  its isolated run collected 243 tests: 212 passed and 31 failed. The
  packaging lane's `tests/test_packaging.py` SHA-256 is
  `602b85a35f2138d2397bc4482b411f1a44254e67a69d4e92b10aa57d4ab6d0eb`;
  its isolated run collected 115 tests: 103 passed, 10 failed, and 2
  skipped.
- 2026-08-22 — The orchestrator's joined RED run collected 358 tests: 315
  passed, 41 failed, and 2 skipped. Every failure identifies a missing or
  stale README, example, or CHANGELOG deliverable. The candidate corrections
  addressed the failure review's three High and two Medium findings. At that
  RED checkpoint, closure still awaited fresh independent senior re-review,
  and the joined RED remained intentionally uncommitted.
- 2026-08-23 — Corrected the packaging import sentinel by removing only the
  invalid package-level `request` monkeypatch; the real public-module request,
  ADC, storage-client, GCS-lease, DNS, and socket sentinels remained active.
  The isolated sentinel passed, and `tests/test_packaging.py` was refrozen at
  SHA-256 `ac9dd62493642004a9f8986bb618b92f8522ff8fce6258635f3fae5cd8f58be1`.
  Files: `tests/test_packaging.py`.
- 2026-08-23 — Completed the bounded README, additive CHANGELOG, and import-safe
  six-operation GCS example. The first GREEN join passed 356 tests with 2
  existing skips; scoped lint, example compilation, diff, and JSON checks were
  green, with no live GCP or network action. Files: `README.md`, `CHANGELOG.md`,
  `examples/gcs_example.py`.
- 2026-08-23 — Independent GREEN review iteration 1 reported 0 Critical, 2
  High, and 1 Medium findings: direction-reversed documentation claims could
  pass, a locally shadowed request binding could pass the example gates, and
  ticket/index state was stale. Files: `tests/test_docs.py`,
  `tests/test_packaging.py`, this ticket, and the local ticket index.
- 2026-08-23 — Closed the documentation-oracle defect: all 18 individual
  reversed-claim mutations are rejected, the truthful documentation suite is
  243/243, and `tests/test_docs.py` is refrozen at SHA-256
  `2954334537979a54826ace9b7b14ed26e07efc740398b8ec3bbdc2b3459556eb`.
  Files: `tests/test_docs.py`.
- 2026-08-23 — Closed the public-request-binding oracle defect: the local
  shadow mutation now fails, the packaging suite passes 114 tests with 2
  existing skips, and `tests/test_packaging.py` is refrozen at SHA-256
  `68a98582b2e81f8f28ec96d24d9877b27ab4701a8b3af25f9d317b33619c7c12`.
  Files: `tests/test_packaging.py`.
- 2026-08-23 — Current orchestrator join passes 357 tests with 2 existing
  skips; scoped lint, example compilation, diff, and ticket-index JSON checks
  are green. The seven-file AGW-52 candidate remains uncommitted and no live
  GCP, network, CI, deployment, tag, or release action occurred. Files:
  `README.md`, `CHANGELOG.md`, `examples/gcs_example.py`,
  `tests/test_docs.py`, `tests/test_packaging.py`, this ticket, and the local
  ticket index.
- 2026-08-23 — Final targeted re-review VERIFIED with 0 Critical, 0 High, 0
  Medium, and 0 Low findings. All 10/10 truthful nodes passed; 18/18
  documentation mutations and 3/3 public-request-binding attacks were
  rejected. The runtime sentinel observed six public request awaits and zero
  ADC, storage-client, GCS-lease, DNS, or socket calls. The joined suite remains
  357 passed with 2 existing skips. The ticket and index remain IN PROGRESS,
  AGW-32 remains OPEN, and the index diff affects only AGW-52. Files: this
  ticket and the local ticket index.
- 2026-08-23 — Moved to IN REVIEW after product commit
  `a768c8f806ecec4fedf120136f8806c3689c3707` and an independent contribution
  audit mapped all 11/11 obligations PASS with 0 missing and 0 Critical, 0 High,
  and 0 Medium findings. OPEN-GLOBAL before feature acceptance: build wheel and
  sdist artifacts and verify clean install/import; run the full suite, shuffled
  order, and Python 3.10–3.14 interpreter matrix; prove repo-wide and every
  changed production module at 100% statement and branch coverage with the
  required behavioral matrices; and complete code-review, contract, test,
  security, pipeline, observability, and acceptance gates. Files: this ticket
  and the local ticket index.
