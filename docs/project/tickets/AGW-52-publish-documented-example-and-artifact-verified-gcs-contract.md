# AGW-52: Publish documented example and artifact-verified GCS contract

- **Status:** OPEN
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
